from AccessControl.SecurityManagement import getSecurityManager
from Acquisition import aq_base
from Acquisition.interfaces import IAcquirer
from base64 import b64decode
from email.utils import formatdate
from fnmatch import fnmatch
from plone.rest.interfaces import ICORSPolicy
from plone.restapi.bbb import base_hasattr
from plone.restapi.bbb import safe_hasattr
from plone.restapi.exceptions import DeserializationError
from plone.restapi.interfaces import IDeserializeFromJson
from plone.restapi.services import Service
from plone.restapi.services.content.utils import add
from plone.restapi.services.content.utils import create
from plone.rfc822.interfaces import IPrimaryFieldInfo
from Products.CMFCore.utils import getToolByName
from uuid import uuid4
from zExceptions import Unauthorized
from zope.component import queryMultiAdapter
from zope.event import notify
from zope.interface import implementer
from zope.lifecycleevent import ObjectCreatedEvent
from zope.publisher.interfaces import IPublishTraverse
from zope.publisher.interfaces import NotFound
from ZODB.blob import rename_or_copy_blob

import json
import logging
import os
import time


logger = logging.getLogger(__name__)


_S3_MIN_PART_SIZE = 5 * 1024 * 1024  # S3 multipart minimum (non-final parts)
_S3_DEFAULT_PART_SIZE = 8 * 1024 * 1024


def _s3_part_size():
    """Flush threshold for buffered parts. Env-overridable; floored at 5 MiB."""
    raw = os.environ.get("TUS_S3_PART_SIZE_BYTES")
    if not raw:
        return _S3_DEFAULT_PART_SIZE
    try:
        size = int(raw)
    except ValueError:
        return _S3_DEFAULT_PART_SIZE
    return max(size, _S3_MIN_PART_SIZE)


def _resolve_s3_blob_storage(context):
    """Return (storage, s3_client) if the context's ZODB storage is an
    S3BlobStorage, else None. Import-guarded so environments without
    zodb-s3blobs installed continue to work.

    Returns the **Connection's** MVCC instance of the storage (``jar._storage``),
    not ``db.storage``. ZODB's ``DB.open()`` calls ``new_instance()`` on the
    storage for each connection, and commits happen through that per-connection
    instance. The S3BlobStorage keeps pending/staged state on the instance, so
    registrations made on ``db.storage`` (the primary) would be invisible at
    commit time on the per-connection instance.
    """
    try:
        from zodb_s3blobs.storage import S3BlobStorage
    except ImportError:
        logger.debug("TUS/S3: zodb_s3blobs not installed; staying in local mode")
        return None
    jar = getattr(aq_base(context), "_p_jar", None)
    if jar is None:
        logger.debug("TUS/S3: context has no _p_jar; staying in local mode")
        return None
    storage = getattr(jar, "_storage", None)
    if storage is None:
        db = jar.db()
        storage = db.storage if db is not None else None
    if not isinstance(storage, S3BlobStorage):
        logger.debug(
            "TUS/S3: storage %r is not S3BlobStorage; staying in local mode",
            type(storage).__name__,
        )
        return None
    logger.debug(
        "TUS/S3: resolved per-connection S3BlobStorage id=%s", id(storage)
    )
    return storage, storage._s3_client


TUS_OPTIONS_RESPONSE_HEADERS = {
    "Tus-Resumable": "1.0.0",
    "Tus-Version": "1.0.0",
    "Tus-Extension": "creation,expiration",
}


class UploadOptions(Service):
    """TUS upload endpoint for handling OPTIONS requests without CORS."""

    def reply(self):
        for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
            self.request.response.setHeader(name, value)
        return super().reply()


class TUSBaseService(Service):
    def __call__(self):
        # We need to add additional TUS headers if this is a CORS preflight
        # request.
        policy = queryMultiAdapter((self.context, self.request), ICORSPolicy)
        if policy is not None:
            if self.request._rest_cors_preflight:
                policy.process_preflight_request()
                # Add TUS options headers in addition to CORS headers
                for name, value in TUS_OPTIONS_RESPONSE_HEADERS.items():
                    self.request.response.setHeader(name, value)
                return
            else:
                policy.process_simple_request()
        else:
            if self.request._rest_cors_preflight:
                return

        return self.render()

    def check_tus_version(self):
        version = self.request.getHeader("Tus-Resumable")
        if version != "1.0.0":
            return False
        return True

    def unsupported_version(self):
        self.request.response.setHeader("Tus-Version", "1.0.0")
        self.request.response.setStatus(412)
        return {
            "error": {"type": "Precondition Failed", "message": "Unsupported version"}
        }

    def error(self, type, message, status=400):
        """
        Set a status code (400 is the default error in the TUS
        reference server implementation) and return a plone.restapi
        conform error body.
        """
        self.request.response.setStatus(status)
        return {"error": {"type": type, "message": message}}


class UploadPost(TUSBaseService):
    """TUS upload endpoint for creating a new upload resource."""

    def reply(self):
        if not self.check_tus_version():
            return self.unsupported_version()

        length = self.request.getHeader("Upload-Length", "")
        try:
            length = int(length)
        except ValueError:
            return self.error("Bad Request", "Missing or invalid Upload-Length header")

        # Parse metadata
        metadata = {}
        for item in self.request.getHeader("Upload-Metadata", "").split(","):
            key_value = item.split()
            if len(key_value) == 2:
                key = key_value[0].lower()
                value = b64decode(key_value[1]).decode("utf-8")
                metadata[key] = value
        metadata["length"] = length
        if self.__name__.endswith("@tus-replace"):
            metadata["mode"] = "replace"
        else:
            metadata["mode"] = "create"

        tus_upload = TUSUpload(uuid4().hex, metadata=metadata)

        resolved = _resolve_s3_blob_storage(self.context)
        if resolved is not None:
            tus_upload.initialize_s3(*resolved)

        self.request.response.setStatus(201)
        url = self.request.getURL()
        # Regardless of @tus-upload or @tus-replace the response should return
        # a Location of @tus-upload
        if url.endswith("@tus-replace"):
            url = url.replace("@tus-replace", "@tus-upload")
        self.request.response.setHeader(
            "Location",
            f"{url}/{tus_upload.uid}",
        )
        self.request.response.setHeader("Upload-Expires", tus_upload.expires())
        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        return super().reply()


@implementer(IPublishTraverse)
class UploadFileBase(TUSBaseService):
    def __init__(self, context, request):
        super().__init__(context, request)
        self.uid = None

    def publishTraverse(self, request, name):
        if self.uid is None:
            self.uid = name
        else:
            raise NotFound(self, name, request)
        return self

    def tus_upload(self):
        if self.uid is None:
            return

        tus_upload = TUSUpload(self.uid)
        length = tus_upload.length()
        if length == 0:
            return

        if tus_upload.s3_mode:
            resolved = _resolve_s3_blob_storage(self.context)
            if resolved is not None:
                tus_upload.attach_s3_runtime(*resolved)

        return tus_upload

    def check_add_modify_permission(self, mode):
        sm = getSecurityManager()
        if mode == "create":
            if not sm.checkPermission("Add portal content", self.context):
                raise Unauthorized
        else:
            if not sm.checkPermission("Modify portal content", self.context):
                raise Unauthorized


class UploadHead(UploadFileBase):
    """TUS upload endpoint for handling HEAD requests"""

    def reply(self):

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        if not self.check_tus_version():
            return self.unsupported_version()

        self.request.response.setHeader("Upload-Length", f"{tus_upload.length()}")
        self.request.response.setHeader("Upload-Offset", f"{tus_upload.offset()}")
        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        self.request.response.setHeader("Cache-Control", "no-store")
        self.request.response.setStatus(200, lock=1)
        return super().reply()


@implementer(IPublishTraverse)
class UploadPatch(UploadFileBase):
    """TUS upload endpoint for handling PATCH requests"""

    def reply(self):

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        if not self.check_tus_version():
            return self.unsupported_version()

        content_type = self.request.getHeader("Content-Type")
        if content_type != "application/offset+octet-stream":
            return self.error("Bad Request", "Missing or invalid Content-Type header")

        offset = self.request.getHeader("Upload-Offset", "")
        try:
            offset = int(offset)
        except ValueError:
            return self.error("Bad Request", "Missing or invalid Upload-Offset header")

        request_body = self.request._file
        if hasattr(request_body, "raw"):  # Unwrap io.BufferedRandom
            request_body = request_body.raw
        tus_upload.write(request_body, offset)
        offset = tus_upload.offset()

        if tus_upload.finished:
            self.create_or_modify_content(tus_upload)
        else:
            self.request.response.setHeader("Upload-Expires", tus_upload.expires())

        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        self.request.response.setHeader("Upload-Offset", f"{offset}")
        return self.reply_no_content()

    def create_or_modify_content(self, tus_upload):
        metadata = tus_upload.metadata()
        filename = metadata.get("filename", "")
        content_type = metadata.get("content-type", "application/octet-stream")
        mode = metadata.get("mode", "create")
        fieldname = metadata.get("fieldname")

        if mode == "create":
            type_ = metadata.get("@type")
            if type_ is None:
                ctr = getToolByName(self.context, "content_type_registry")
                type_ = ctr.findTypeName(filename.lower(), content_type, "") or "File"

            obj = create(self.context, type_)
        else:
            obj = self.context

        if not fieldname:
            info = IPrimaryFieldInfo(obj, None)
            if info is not None:
                fieldname = info.fieldname
            elif base_hasattr(obj, "getPrimaryField"):
                field = obj.getPrimaryField()
                fieldname = field.getName()

        if not fieldname:
            return self.error("Bad Request", "Fieldname required", 400)

        # Acquisition wrap temporarily for deserialization
        temporarily_wrapped = False
        if IAcquirer.providedBy(obj) and not safe_hasattr(obj, "aq_base"):
            obj = obj.__of__(self.context)
            temporarily_wrapped = True

        # Update field with file data
        deserializer = queryMultiAdapter((obj, self.request), IDeserializeFromJson)
        if deserializer is None:
            return self.error(
                "Not Implemented",
                f"Cannot deserialize type {obj.portal_type}",
                501,
            )
        try:
            deserializer(data={fieldname: tus_upload}, create=mode == "create")
        except DeserializationError as e:
            return self.error("Deserialization Error", str(e), 400)

        if temporarily_wrapped:
            obj = aq_base(obj)

        if mode == "create":
            if not getattr(deserializer, "notifies_create", False):
                notify(ObjectCreatedEvent(obj))
            obj = add(self.context, obj)

        tus_upload.close()
        tus_upload.cleanup()
        self.request.response.setHeader("Location", obj.absolute_url())


class TUSUpload:

    file_prefix = "tus_upload_"
    expiration_period = 60 * 60
    finished = False

    def __init__(self, uid, metadata=None):
        self.uid = uid

        self.tmp_dir = os.environ.get("TUS_TMP_FILE_DIR")
        if self.tmp_dir is None:
            client_home = os.environ.get("CLIENT_HOME")
            self.tmp_dir = os.path.join(client_home, "tus-uploads")
        if not os.path.isdir(self.tmp_dir):
            os.makedirs(self.tmp_dir)

        self.filepath = os.path.join(self.tmp_dir, self.file_prefix + self.uid)
        self.metadata_path = self.filepath + ".json"
        # Per-upload in-progress S3 part buffer (separate from self.filepath)
        self.part_buffer_path = self.filepath + ".part"
        self._metadata = None

        # Runtime-only S3 refs (not persisted; re-attached on each request)
        self._s3_storage = None
        self._s3_client = None
        # Set once process_blob has handed the staging key to the storage
        self._handoff_done = False

        if metadata is not None:
            self.initalize(metadata)

        self._file = None

    # ----- metadata + persisted S3 state -----

    def initalize(self, metadata):
        """Initialize a new TUS upload by writing its metadata to disk."""
        self.cleanup_expired()
        with open(self.metadata_path, "w") as f:
            json.dump(metadata, f)
        self._metadata = metadata

    def metadata(self):
        """Returns the metadata of the current upload."""
        if self._metadata is None:
            if os.path.exists(self.metadata_path):
                with open(self.metadata_path, "rb") as f:
                    self._metadata = json.load(f)
        return self._metadata or {}

    def _save_metadata(self):
        if self._metadata is None:
            return
        tmp_path = self.metadata_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(self._metadata, f)
        os.replace(tmp_path, self.metadata_path)

    def _s3_state(self):
        return self.metadata().setdefault("_s3_state", {})

    @property
    def s3_mode(self):
        return bool(self.metadata().get("_s3_state", {}).get("enabled"))

    @property
    def staging_key(self):
        return self._s3_state().get("staging_key")

    @property
    def multipart_upload_id(self):
        return self._s3_state().get("multipart_upload_id")

    @property
    def parts(self):
        return self._s3_state().setdefault("parts", [])

    @property
    def next_part_number(self):
        return self._s3_state().get("next_part_number", 1)

    @property
    def total_uploaded_bytes(self):
        return self._s3_state().get("total_uploaded_bytes", 0)

    # ----- S3 initialisation / attachment -----

    def initialize_s3(self, s3_storage, s3_client):
        """Create the S3 multipart upload and persist per-upload state.

        Called once by UploadPost after a fresh TUSUpload is created and the
        target context's storage has been identified as S3-backed.
        """
        staging_key = f"tus-staging/{self.uid}"
        upload_id = s3_client.create_multipart_upload(staging_key)
        state = self._s3_state()
        state["enabled"] = True
        state["staging_key"] = staging_key
        state["multipart_upload_id"] = upload_id
        state["parts"] = []
        state["next_part_number"] = 1
        state["total_uploaded_bytes"] = 0
        self._save_metadata()
        self._s3_storage = s3_storage
        self._s3_client = s3_client
        logger.info(
            "TUS/S3: initialised multipart upload uid=%s staging_key=%s "
            "upload_id=%s",
            self.uid,
            staging_key,
            upload_id,
        )

    def attach_s3_runtime(self, s3_storage, s3_client):
        """Re-attach runtime S3 refs after a worker-local TUSUpload rehydration."""
        self._s3_storage = s3_storage
        self._s3_client = s3_client

    # ----- length / offset / expiration -----

    def length(self):
        """Returns the total upload length."""
        metadata = self.metadata()
        if "length" in metadata:
            return metadata["length"]
        return 0

    def offset(self):
        """Returns the current offset."""
        if self.s3_mode:
            buffered = 0
            if os.path.exists(self.part_buffer_path):
                buffered = os.path.getsize(self.part_buffer_path)
            return self.total_uploaded_bytes + buffered
        if os.path.exists(self.filepath):
            return os.path.getsize(self.filepath)
        return 0

    def expires(self):
        """Returns the expiration time of the current upload."""
        stat_path = None
        if self.s3_mode and os.path.exists(self.part_buffer_path):
            stat_path = self.part_buffer_path
        elif os.path.exists(self.filepath):
            stat_path = self.filepath
        if stat_path is not None:
            expiration = os.stat(stat_path).st_mtime + self.expiration_period
        else:
            expiration = time.time() + self.expiration_period
        return formatdate(expiration, False, True)

    # ----- write / finalize -----

    def write(self, infile, offset=0):
        """Write to uploaded file at the given offset."""
        if self.s3_mode:
            self._write_s3(infile)
            return

        mode = "wb"
        if os.path.exists(self.filepath):
            mode = "ab+"
        with open(self.filepath, mode) as f:
            f.seek(offset)
            while True:
                chunk = infile.read(2 << 16)
                if not chunk:
                    offset = f.tell()
                    break
                f.write(chunk)
        length = self.length()
        if length and offset >= length:
            self.finished = True

    def _write_s3(self, infile):
        """Append chunk bytes into the part buffer, flushing when full."""
        if self._s3_client is None:
            raise RuntimeError(
                "TUSUpload is in S3 mode but no S3 client is attached. "
                "Call attach_s3_runtime(...) before writing."
            )
        flush_threshold = _s3_part_size()
        length = self.length()
        with open(self.part_buffer_path, "ab") as f:
            while True:
                chunk = infile.read(2 << 16)
                if not chunk:
                    break
                f.write(chunk)
                if f.tell() >= flush_threshold:
                    # Close before flush so the path can be re-opened for read
                    f.close()
                    self._flush_part(final=False)
                    f = open(self.part_buffer_path, "ab")
        # End of this PATCH body — decide whether to finalise.
        total_after = self.total_uploaded_bytes
        buf_size = (
            os.path.getsize(self.part_buffer_path)
            if os.path.exists(self.part_buffer_path)
            else 0
        )
        total_after += buf_size
        if length and total_after >= length:
            # Final part (any size allowed)
            if buf_size > 0:
                self._flush_part(final=True)
            self._complete_multipart()
            self.finished = True

    def _flush_part(self, final):
        """Upload the current part buffer as one S3 UploadPart and reset it."""
        if not os.path.exists(self.part_buffer_path):
            return
        size = os.path.getsize(self.part_buffer_path)
        if size == 0:
            os.remove(self.part_buffer_path)
            return
        state = self._s3_state()
        part_number = state["next_part_number"]
        with open(self.part_buffer_path, "rb") as body:
            etag = self._s3_client.upload_part(
                self.staging_key,
                self.multipart_upload_id,
                part_number,
                body,
            )
        state["parts"].append(
            {"PartNumber": part_number, "ETag": etag, "Size": size}
        )
        state["next_part_number"] = part_number + 1
        state["total_uploaded_bytes"] = state.get("total_uploaded_bytes", 0) + size
        self._save_metadata()
        os.remove(self.part_buffer_path)
        logger.debug(
            "TUS/S3: flushed part %d (%d bytes) for upload %s final=%s",
            part_number,
            size,
            self.uid,
            final,
        )

    def _complete_multipart(self):
        self._s3_client.complete_multipart_upload(
            self.staging_key,
            self.multipart_upload_id,
            self.parts,
        )
        logger.info(
            "TUS/S3: completed multipart upload uid=%s parts=%d "
            "total_bytes=%d staging_key=%s",
            self.uid,
            len(self.parts),
            self.total_uploaded_bytes,
            self.staging_key,
        )

    # ----- open/close/cleanup -----

    def open(self):
        """Open the uploaded file for reading and return it."""
        if self._file is None or self._file.closed:
            self._file = open(self.filepath, "rb")
        return self._file

    def close(self):
        """Close the uploaded file."""
        if self._file is not None and not self._file.closed:
            self._file.close()

    def cleanup(self):
        """Remove temporary upload files and abort any dangling S3 state."""
        if self.s3_mode:
            # If the upload was abandoned (never completed), abort multipart.
            if not self.finished and self.multipart_upload_id and self._s3_client:
                try:
                    self._s3_client.abort_multipart_upload(
                        self.staging_key, self.multipart_upload_id
                    )
                except Exception:
                    logger.warning(
                        "TUS/S3: failed to abort multipart for upload %s",
                        self.uid,
                        exc_info=True,
                    )
            # If we completed but never handed off, delete the staging object.
            if self.finished and not self._handoff_done and self._s3_client:
                try:
                    self._s3_client.delete_object(self.staging_key)
                except Exception:
                    logger.warning(
                        "TUS/S3: failed to delete staging key %s after "
                        "incomplete handoff for upload %s",
                        self.staging_key,
                        self.uid,
                        exc_info=True,
                    )
            if os.path.exists(self.part_buffer_path):
                os.remove(self.part_buffer_path)
        if os.path.exists(self.filepath):
            os.remove(self.filepath)
        if os.path.exists(self.metadata_path):
            os.remove(self.metadata_path)

    def cleanup_expired(self):
        """Cleanup unfinished uploads that have expired."""
        for filename in os.listdir(self.tmp_dir):
            if fnmatch(filename, "tus_upload_*.json"):
                metadata_path = os.path.join(self.tmp_dir, filename)
                filepath = metadata_path[:-5]
                part_buffer_path = filepath + ".part"
                mtime_src = None
                for candidate in (part_buffer_path, filepath, metadata_path):
                    if os.path.exists(candidate):
                        mtime_src = candidate
                        break
                if mtime_src is None:
                    continue
                mtime = os.stat(mtime_src).st_mtime
                if (time.time() - mtime) <= self.expiration_period:
                    continue
                # Expired: best-effort S3 cleanup if this was an S3-mode upload.
                try:
                    with open(metadata_path, "rb") as f:
                        expired_meta = json.load(f)
                except (OSError, ValueError):
                    expired_meta = {}
                s3_state = expired_meta.get("_s3_state") or {}
                if s3_state.get("enabled") and self._s3_client is not None:
                    try:
                        self._s3_client.abort_multipart_upload(
                            s3_state.get("staging_key"),
                            s3_state.get("multipart_upload_id"),
                        )
                    except Exception:
                        logger.debug(
                            "TUS/S3: expired-upload abort failed (key=%s)",
                            s3_state.get("staging_key"),
                            exc_info=True,
                        )
                os.remove(metadata_path)
                for extra in (filepath, part_buffer_path):
                    if os.path.exists(extra):
                        os.remove(extra)

    # ----- handoff to ZODB blob -----

    def process_blob(self, blob):
        if self.s3_mode:
            if self._s3_storage is None:
                raise RuntimeError(
                    "TUSUpload in S3 mode missing storage reference at handoff"
                )
            size = self.length()
            # Write a tiny marker file and rename it into _p_blob_uncommitted,
            # matching the normal rename_or_copy_blob flow. The marker's
            # contents are diagnostic only; authority is the registration.
            marker_source = self.filepath + ".marker"
            marker_content = (
                f"S3BLOB-STAGED\n{self.staging_key}\n{size}\n".encode()
            )
            with open(marker_source, "wb") as f:
                f.write(marker_content)
            target_path = blob._p_blob_uncommitted
            rename_or_copy_blob(marker_source, target_path)
            self._s3_storage.register_staged_s3_key(
                target_path, self.staging_key, size
            )
            self._handoff_done = True
            logger.info(
                "TUS/S3: handoff complete uid=%s staging_key=%s size=%d "
                "marker_path=%s storage_id=%s",
                self.uid,
                self.staging_key,
                size,
                target_path,
                id(self._s3_storage),
            )
            return
        rename_or_copy_blob(self.filepath, blob._p_blob_uncommitted)

