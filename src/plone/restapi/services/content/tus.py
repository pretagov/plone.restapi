from AccessControl.SecurityManagement import getSecurityManager
from Acquisition import aq_base
from Acquisition.interfaces import IAcquirer
from base64 import b64decode
from BTrees.OOBTree import OOBTree
from email.utils import formatdate
from fnmatch import fnmatch
from io import BytesIO
from persistent.mapping import PersistentMapping
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
from tempfile import mkstemp
from uuid import uuid4
from zExceptions import Unauthorized
from zope.annotation.interfaces import IAnnotations
from zope.component import queryMultiAdapter
from zope.event import notify
from zope.interface import alsoProvides
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


# Annotation key on the upload's container (folder/site root). Holds an
# OOBTree[uid → PersistentMapping(descriptor)] for in-progress S3 uploads.
ANNOTATION_KEY = "plone.restapi.tus_uploads"

# S3 staging key prefix and lifecycle rule ID for auto-aborting abandoned
# multipart uploads. Kept aligned with the manual setup instructions
# documented in CLAUDE.md.
TUS_STAGING_PREFIX = "tus-staging/"
TUS_LIFECYCLE_RULE_ID = "tus-staging-abort"
TUS_LIFECYCLE_DAYS = 7

# Feature flag controlling the nginx-mediated direct-to-S3 offload.
# When unset/false, the @tus-authorize endpoint always returns
# X-Route: plone, which keeps PATCH bodies flowing through the Zope worker
# (the legacy/safe path). When true, @tus-authorize hands nginx a
# presigned UploadPart URL for non-final chunks; only the final chunk goes
# back through Plone for the transactional CompleteMultipartUpload +
# content-creation step. See the deployment doc for nginx config required
# to make use of this.
TUS_S3_OFFLOAD_ENV = "TUS_S3_OFFLOAD_ENABLED"
TUS_PRESIGNED_URL_EXPIRES = 300  # seconds — generous for slow uploads


def _s3_offload_enabled():
    return os.environ.get(TUS_S3_OFFLOAD_ENV, "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

# Process-local set of bucket names already checked / had the rule applied
# during this process's lifetime. Auto-apply runs at most once per bucket
# per process, regardless of success — never block uploads on lifecycle.
_LIFECYCLE_APPLIED: set = set()


def _ensure_lifecycle_rule_once(s3_client):
    """Apply the abort-multipart lifecycle rule on first POST per process.

    Idempotent within a process; tolerant of failure (logs a warning and
    moves on). The S3Client method itself is idempotent against the bucket
    too, so even repeated calls would be safe — this just avoids the round
    trip after the first call.
    """
    bucket = getattr(s3_client, "bucket_name", None)
    if not bucket or bucket in _LIFECYCLE_APPLIED:
        return
    try:
        s3_client.ensure_abort_multipart_lifecycle_rule(
            rule_id=TUS_LIFECYCLE_RULE_ID,
            prefix=TUS_STAGING_PREFIX,
            days=TUS_LIFECYCLE_DAYS,
        )
    except Exception:
        logger.warning(
            "TUS/S3: lifecycle setup raised unexpectedly for bucket=%s; "
            "continuing without it.",
            bucket,
            exc_info=True,
        )
    # Mark applied either way so we don't retry on every POST.
    _LIFECYCLE_APPLIED.add(bucket)


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


def _container_uploads(container, create=False):
    """Return the OOBTree of in-progress S3 uploads for this container.

    If ``create`` is False and no annotation exists, returns None.
    """
    annotations = IAnnotations(container)
    uploads = annotations.get(ANNOTATION_KEY)
    if uploads is None:
        if not create:
            return None
        uploads = OOBTree()
        annotations[ANNOTATION_KEY] = uploads
    return uploads


TUS_OPTIONS_RESPONSE_HEADERS = {
    "Tus-Resumable": "1.0.0",
    "Tus-Version": "1.0.0",
    "Tus-Extension": "creation,expiration,termination",
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

    def disable_csrf_protection(self):
        """Mark the request as auto-CSRF-exempt.

        plone.protect's auto-CSRF check fires whenever a request triggers a
        ZODB write. The annotation-backed S3 path writes the upload
        descriptor on POST and updates ``last_active`` on every PATCH; the
        DELETE path removes the annotation. None of these requests carry a
        plone.protect token (the TUS protocol has no provision for one),
        and the endpoints already require Bearer/Basic auth — so the
        protection is redundant. Mirrors the pattern used in
        ``plone.restapi.services.upgrade.post`` and similar services.
        """
        import plone.protect.interfaces

        if "IDisableCSRFProtection" in dir(plone.protect.interfaces):
            alsoProvides(
                self.request,
                plone.protect.interfaces.IDisableCSRFProtection,
            )


class UploadPost(TUSBaseService):
    """TUS upload endpoint for creating a new upload resource."""

    def reply(self):
        if not self.check_tus_version():
            return self.unsupported_version()

        self.disable_csrf_protection()

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

        uid = uuid4().hex
        resolved = _resolve_s3_blob_storage(self.context)
        if resolved is not None:
            _ensure_lifecycle_rule_once(resolved[1])
            tus_upload = S3TUSUpload(
                uid,
                container=self.context,
                metadata=metadata,
                s3_storage=resolved[0],
                s3_client=resolved[1],
            )
        else:
            tus_upload = TUSUpload(uid, metadata=metadata)

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
            logger.warning(
                "TUS: %s request with no uid in URL (context=%s)",
                self.request.get("REQUEST_METHOD", "?"),
                "/".join(self.context.getPhysicalPath()),
            )
            return

        # Prefer the annotation-backed (S3) path: state is shared across
        # workers via ZODB, no stickiness required.
        uploads = _container_uploads(self.context)
        if uploads is not None and self.uid in uploads:
            resolved = _resolve_s3_blob_storage(self.context)
            if resolved is None:
                # Annotation says S3 but storage no longer reports as S3. This
                # would be a configuration regression rather than a routing
                # miss, so log loudly.
                logger.error(
                    "TUS: annotation entry exists for uid=%s but S3 storage "
                    "did not resolve. context=%s",
                    self.uid,
                    "/".join(self.context.getPhysicalPath()),
                )
                return
            return S3TUSUpload(
                self.uid,
                container=self.context,
                s3_storage=resolved[0],
                s3_client=resolved[1],
            )

        # Local-disk fallback (dev/test or non-S3 deployments).
        tus_upload = TUSUpload(self.uid)
        length = tus_upload.length()
        if length == 0:
            logger.warning(
                "TUS: upload uid not found. uid=%s method=%s context=%s "
                "tmp_dir=%s metadata_exists=%s file_exists=%s "
                "annotation_keys=%s",
                self.uid,
                self.request.get("REQUEST_METHOD", "?"),
                "/".join(self.context.getPhysicalPath()),
                tus_upload.tmp_dir,
                os.path.exists(tus_upload.metadata_path),
                os.path.exists(tus_upload.filepath),
                list(uploads.keys()) if uploads is not None else None,
            )
            return

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

        self.disable_csrf_protection()

        content_type = self.request.getHeader("Content-Type")
        if content_type != "application/offset+octet-stream":
            return self.error("Bad Request", "Missing or invalid Content-Type header")

        offset = self.request.getHeader("Upload-Offset", "")
        try:
            offset = int(offset)
        except ValueError:
            return self.error("Bad Request", "Missing or invalid Upload-Offset header")

        content_length_raw = self.request.getHeader("Content-Length", "")
        try:
            content_length = int(content_length_raw) if content_length_raw else None
        except ValueError:
            content_length = None

        request_body = self.request._file
        if hasattr(request_body, "raw"):  # Unwrap io.BufferedRandom
            request_body = request_body.raw
        try:
            tus_upload.write(
                request_body, offset, content_length=content_length
            )
        except TUSUploadError as e:
            return self.error(e.error_type, str(e), e.status)
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


@implementer(IPublishTraverse)
class UploadAuthorize(UploadFileBase):
    """Authorise a single TUS PATCH chunk for direct-to-S3 forwarding.

    This is *not* part of the TUS protocol — it's an internal endpoint used
    by nginx's ``auth_request`` directive in the data-plane offload setup.
    nginx makes a body-less GET subrequest here for every PATCH the client
    sends; the response headers tell nginx whether to:

    * stream the body straight to S3 via a presigned ``UploadPart`` URL
      (``X-Route: s3`` + ``X-S3-Url``), or
    * forward the request to Plone unchanged (``X-Route: plone``),
      which is the path the final chunk takes so Plone can run
      ``CompleteMultipartUpload`` + content creation transactionally.

    Behaviour is controlled by the ``TUS_S3_OFFLOAD_ENABLED`` env var. When
    disabled (default), every chunk routes back to Plone — equivalent to
    not having the offload at all, used as a kill switch.

    The endpoint is auth-checked exactly like the other TUS handlers
    (``Add portal content`` / ``Modify portal content``), so a leaked
    ``@tus-authorize`` URL conveys no more privilege than a leaked
    ``@tus-upload`` URL.
    """

    def _route_to_plone(self, *, reason=None):
        """Build a 'send the body to Plone' authorize response."""
        self.request.response.setHeader("X-Route", "plone")
        if reason:
            self.request.response.setHeader("X-Tus-Authorize-Reason", reason)
        self.request.response.setStatus(200, lock=1)
        logger.debug(
            "TUS/S3: authorize routed to Plone uid=%s reason=%s",
            getattr(self, "uid", None),
            reason,
        )
        return ""

    def _original_int_header(self, *names):
        """Return the first parseable header value from ``names``, or None.

        nginx auth_request sub-requests have no original body, so
        ``Upload-Offset`` / ``Content-Length`` are forwarded under
        ``X-Original-*`` aliases by the nginx config. Accept either name
        so the endpoint is callable for ad-hoc testing too.
        """
        for name in names:
            raw = self.request.getHeader(name, "")
            if raw == "" or raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return None

    def reply(self):
        # If offload is disabled, route everything back to Plone — this
        # makes the endpoint safe to deploy ahead of the nginx config and
        # acts as a runtime kill switch.
        if not _s3_offload_enabled():
            return self._route_to_plone(reason="offload-disabled")

        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        # Local-disk uploads can't be offloaded — there's no S3 multipart
        # to PUT into. Route to Plone.
        if not isinstance(tus_upload, S3TUSUpload):
            return self._route_to_plone(reason="local-mode")

        offset = self._original_int_header("X-Original-Upload-Offset", "Upload-Offset")
        content_length = self._original_int_header(
            "X-Original-Content-Length", "Content-Length"
        )
        if offset is None or content_length is None:
            return self.error(
                "Bad Request",
                "Missing or invalid Upload-Offset / Content-Length",
            )

        # ``metadata()`` returns a copy of the persisted entry — it
        # contains staging_key, multipart_upload_id, length, chunk_size,
        # and the descriptor fields. We don't mutate it here.
        entry = tus_upload.metadata()
        if not entry:
            return self.error("Not Found", "", 404)

        length = int(entry["length"])
        chunk_size = entry.get("chunk_size")
        is_final = (offset + content_length) >= length

        # Validate alignment / size against the established chunk_size.
        # First PATCH learns chunk_size — we don't reject it here, but we
        # do need to know the size to compute a part_number, so we accept
        # the client's Content-Length as the chunk size for this request.
        if chunk_size is None:
            if not is_final and content_length < 5 * 1024 * 1024:
                return self.error(
                    "Bad Request",
                    "Non-final chunk must be at least 5 MiB",
                )
            effective_chunk_size = content_length
        else:
            if offset % chunk_size != 0:
                return self.error(
                    "Bad Request",
                    f"Upload-Offset {offset} is not a multiple of "
                    f"chunk_size {chunk_size}",
                )
            if not is_final and content_length != chunk_size:
                return self.error(
                    "Bad Request",
                    f"Non-final chunk size {content_length} does not match "
                    f"chunk_size {chunk_size}",
                )
            effective_chunk_size = chunk_size

        # Persist chunk_size on the first chunk's authorize, so that when
        # the final chunk arrives at Plone (offload-bypassed up to this
        # point) it has the correct value for part_number calculation.
        # One write per upload — see S3TUSUpload.remember_chunk_size.
        # Only this branch writes, so CSRF only needs disabling here
        # (nginx's auth_request subrequest carries no CSRF token; the
        # endpoint is already gated by the upload's Add/Modify perm).
        if chunk_size is None:
            self.disable_csrf_protection()
            tus_upload.remember_chunk_size(effective_chunk_size)

        # Final chunk goes through Plone — that's where the transactional
        # CompleteMultipartUpload + content-creation work happens.
        if is_final:
            return self._route_to_plone(reason="final-chunk")

        part_number = (offset // effective_chunk_size) + 1
        try:
            url = tus_upload._s3_client.generate_upload_part_presigned_url(
                entry["staging_key"],
                entry["multipart_upload_id"],
                part_number,
                expires_in=TUS_PRESIGNED_URL_EXPIRES,
            )
        except Exception:
            logger.exception(
                "TUS/S3: presigning failed for uid=%s part=%d; falling "
                "back to Plone-routed path for this chunk.",
                self.uid,
                part_number,
            )
            return self._route_to_plone(reason="presign-failed")

        new_offset = offset + content_length
        self.request.response.setHeader("X-Route", "s3")
        self.request.response.setHeader("X-S3-Url", url)
        self.request.response.setHeader("X-Tus-New-Offset", str(new_offset))
        self.request.response.setHeader("X-Tus-Part-Number", str(part_number))
        # Surface whether this was the chunk_size-learning PATCH so an
        # operator inspecting headers can tell. nginx doesn't act on it.
        if chunk_size is None:
            self.request.response.setHeader(
                "X-Tus-Chunk-Size-Learned", str(effective_chunk_size)
            )
        self.request.response.setStatus(200, lock=1)
        logger.debug(
            "TUS/S3: authorize routed to S3 uid=%s part=%d offset=%d size=%d "
            "presigned_expires_in=%d",
            self.uid,
            part_number,
            offset,
            content_length,
            TUS_PRESIGNED_URL_EXPIRES,
        )
        return ""


@implementer(IPublishTraverse)
class UploadDelete(UploadFileBase):
    """TUS upload endpoint for handling DELETE (termination) requests."""

    def reply(self):
        tus_upload = self.tus_upload()
        if tus_upload is None:
            return self.error("Not Found", "", 404)

        metadata = tus_upload.metadata()
        self.check_add_modify_permission(metadata.get("mode", "create"))

        if not self.check_tus_version():
            return self.unsupported_version()

        self.disable_csrf_protection()

        tus_upload.close()
        tus_upload.cleanup()
        self.request.response.setHeader("Tus-Resumable", "1.0.0")
        self.request.response.setStatus(204, lock=1)
        return self.reply_no_content()


class TUSUploadError(Exception):
    """Internal error raised by TUS upload classes during ``write()``.

    Carries enough information for the service layer to translate into a
    well-formed TUS error response.
    """

    def __init__(self, message, error_type="Bad Request", status=400):
        super().__init__(message)
        self.error_type = error_type
        self.status = status


class TUSUpload:
    """Local-disk TUS upload state. Used in dev/test or non-S3 deployments.

    State lives in ``CLIENT_HOME/tus-uploads/`` (or ``$TUS_TMP_FILE_DIR``).
    Bytes accumulate in a single file at ``self.filepath``; metadata is
    written alongside as ``<uid>.json``.
    """

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
        self._metadata = None

        if metadata is not None:
            self.initalize(metadata)

        self._file = None

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

    def length(self):
        """Returns the total upload length."""
        metadata = self.metadata()
        if "length" in metadata:
            return metadata["length"]
        return 0

    def offset(self):
        """Returns the current offset."""
        if os.path.exists(self.filepath):
            return os.path.getsize(self.filepath)
        return 0

    def expires(self):
        """Returns the expiration time of the current upload."""
        if os.path.exists(self.filepath):
            expiration = os.stat(self.filepath).st_mtime + self.expiration_period
        else:
            expiration = time.time() + self.expiration_period
        return formatdate(expiration, False, True)

    def write(self, infile, offset=0, content_length=None):
        """Write to uploaded file at the given offset.

        ``content_length`` is accepted for signature compatibility with
        ``S3TUSUpload.write`` but ignored — the local-disk path drains the
        body until EOF.
        """
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
        """Remove temporary upload files."""
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
                mtime_src = None
                for candidate in (filepath, metadata_path):
                    if os.path.exists(candidate):
                        mtime_src = candidate
                        break
                if mtime_src is None:
                    continue
                mtime = os.stat(mtime_src).st_mtime
                if (time.time() - mtime) <= self.expiration_period:
                    continue
                os.remove(metadata_path)
                if os.path.exists(filepath):
                    os.remove(filepath)

    def process_blob(self, blob):
        """Hand the uploaded bytes off to the ZODB blob's uncommitted file."""
        rename_or_copy_blob(self.filepath, blob._p_blob_uncommitted)


class S3TUSUpload(TUSUpload):
    """Multi-instance-safe TUS upload backed by S3 multipart + ZODB annotation.

    State lives in two places:
    - **Container annotation** (``IAnnotations(container)[ANNOTATION_KEY]``):
      a small descriptor (length, filename, content-type, fieldname, mode,
      multipart_upload_id, staging_key, chunk_size, created, last_active).
      Shared across workers via ZODB; no local disk involvement.
    - **S3 multipart upload**: parts themselves. Source of truth for what's
      been uploaded; recovered via ``list_parts`` for HEAD/Complete.

    Per-PATCH flow is intentionally tiny: the request body streams straight
    into ``upload_part`` with ``part_number = offset / chunk_size + 1``.
    There's no local buffer, no flush threshold, no double-pass over data.
    Sticky routing is unnecessary because any worker can resolve uid →
    annotation → multipart_upload_id and proceed.
    """

    # Subclass init must NOT touch tmp_dir/filepath like the parent does.
    file_prefix = TUSUpload.file_prefix
    expiration_period = TUSUpload.expiration_period

    def __init__(
        self, uid, container, metadata=None, s3_storage=None, s3_client=None
    ):
        # Deliberately bypass TUSUpload.__init__ — it creates a tmp dir we
        # don't need. Set the minimum attributes the parent class methods
        # might still touch.
        self.uid = uid
        self.container = container
        self.tmp_dir = None
        self.filepath = None
        self.metadata_path = None
        self._metadata = None
        self._file = None
        self.finished = False
        self._s3_storage = s3_storage
        self._s3_client = s3_client
        # Set after process_blob hands the staging key to the storage so
        # cleanup() knows whether to delete the object on abort.
        self._handoff_done = False

        if metadata is not None:
            self._initialize(metadata)

    @property
    def s3_mode(self):
        return True

    @property
    def staging_key(self):
        return self._entry().get("staging_key")

    @property
    def multipart_upload_id(self):
        return self._entry().get("multipart_upload_id")

    def _entry(self):
        """Return the OOBTree entry for this upload, or empty mapping."""
        uploads = _container_uploads(self.container)
        if uploads is None:
            return {}
        return uploads.get(self.uid) or {}

    def _save_entry(self, entry):
        uploads = _container_uploads(self.container, create=True)
        uploads[self.uid] = entry

    def remember_chunk_size(self, chunk_size):
        """Persist chunk_size on the upload entry if it isn't set yet.

        Called from the authorize endpoint so that when the data-plane
        offload is on, the first chunk's size is recorded even though
        the PATCH body bypasses Plone. Without this, the first PATCH
        that ever reaches Plone (typically the final chunk, which is
        smaller) would "learn" the wrong chunk_size from its own
        content_length and compute a bogus part_number — leading to
        S3 InvalidArgument on UploadPart.

        One write per upload: gated on chunk_size being None, so every
        chunk after the first finds it already set and does nothing.
        """
        entry = self._entry()
        if not entry:
            return
        if entry.get("chunk_size") is None:
            entry["chunk_size"] = int(chunk_size)
            entry["last_active"] = time.time()
            self._save_entry(entry)
            self._metadata = entry

    def _initialize(self, metadata):
        """Create a fresh multipart upload and persist the descriptor."""
        self.cleanup_expired()
        staging_key = f"{TUS_STAGING_PREFIX}{self.uid}"
        upload_id = self._s3_client.create_multipart_upload(staging_key)
        now = time.time()
        entry = PersistentMapping(
            {
                # Upload descriptor (immutable once written, except chunk_size
                # which is learned from the first PATCH).
                "length": int(metadata.get("length", 0)),
                "filename": metadata.get("filename", ""),
                "content-type": metadata.get(
                    "content-type", "application/octet-stream"
                ),
                "@type": metadata.get("@type"),
                "fieldname": metadata.get("fieldname"),
                "mode": metadata.get("mode", "create"),
                "chunk_size": None,
                # S3 state.
                "staging_key": staging_key,
                "multipart_upload_id": upload_id,
                # Liveness.
                "created": now,
                "last_active": now,
            }
        )
        self._save_entry(entry)
        self._metadata = entry
        logger.info(
            "TUS/S3: created upload uid=%s staging_key=%s upload_id=%s "
            "container=%s length=%d",
            self.uid,
            staging_key,
            upload_id,
            "/".join(self.container.getPhysicalPath()),
            entry["length"],
        )

    def metadata(self):
        """Return the descriptor as a dict (compatible with TUSUpload.metadata)."""
        if self._metadata is None:
            self._metadata = self._entry()
        # The deserializer reads keys like 'content-type', 'filename',
        # '@type', 'mode' off this — they're stored under the same names.
        return dict(self._metadata) if self._metadata else {}

    def length(self):
        return int(self._entry().get("length", 0))

    def offset(self):
        """TUS offset = highest contiguous prefix of part numbers × chunk_size.

        Sparse parts beyond the first gap are ignored — the client will
        re-PATCH from there and S3 will overwrite by part number.

        If the upload has been completed in this request, the multipart
        upload no longer exists in S3 and ``list_parts`` would raise
        ``NoSuchUpload``. Short-circuit to ``length`` in that case — by
        definition a completed upload's offset equals its length.
        """
        if self.finished:
            return self.length()
        entry = self._entry()
        chunk_size = entry.get("chunk_size")
        if not chunk_size:
            return 0
        parts = self._s3_client.list_parts(
            entry["staging_key"], entry["multipart_upload_id"]
        )
        # Highest contiguous run starting from PartNumber=1.
        contiguous = 0
        total = 0
        for part in parts:
            if part["PartNumber"] != contiguous + 1:
                break
            contiguous += 1
            total += part["Size"]
        return total

    def expires(self):
        last_active = self._entry().get("last_active") or time.time()
        return formatdate(last_active + self.expiration_period, False, True)

    def write(self, infile, offset=0, content_length=None):
        """Stream one chunk straight to S3 as a single multipart part."""
        entry = self._entry()
        if not entry:
            raise TUSUploadError(
                f"Upload {self.uid} not found in container annotation",
                error_type="Not Found",
                status=404,
            )

        length = int(entry["length"])
        chunk_size = entry.get("chunk_size")

        if content_length is None:
            raise TUSUploadError(
                "Missing or invalid Content-Length header for PATCH"
            )

        is_final = (offset + content_length) >= length
        if chunk_size is None:
            # Learn chunk_size from the first PATCH.
            if not is_final and content_length < 5 * 1024 * 1024:
                raise TUSUploadError(
                    "Non-final chunk must be at least 5 MiB (S3 multipart "
                    "minimum). Configure the client to use a larger chunkSize."
                )
            chunk_size = content_length
        else:
            if offset % chunk_size != 0:
                raise TUSUploadError(
                    f"Upload-Offset {offset} is not a multiple of "
                    f"chunk_size {chunk_size}"
                )
            if not is_final and content_length != chunk_size:
                raise TUSUploadError(
                    f"Non-final chunk size {content_length} does not match "
                    f"established chunk_size {chunk_size}"
                )

        part_number = (offset // chunk_size) + 1
        # Read exactly content_length bytes into a seekable buffer. boto3's
        # SigV4 signer needs to either rewind the body (to compute SHA256
        # then re-read for upload) or hash a bytes-like payload directly;
        # a streaming reader without seek/tell falls between those paths
        # and triggers `TypeError: object supporting the buffer API required`.
        # The request body is already buffered in Zope's tempfile so this
        # doesn't add real memory pressure beyond the chunk itself.
        data = infile.read(content_length)
        if len(data) != content_length:
            raise TUSUploadError(
                f"Short read on PATCH body: expected {content_length} bytes, "
                f"got {len(data)}"
            )
        body = BytesIO(data)
        etag = self._s3_client.upload_part(
            entry["staging_key"],
            entry["multipart_upload_id"],
            part_number,
            body,
        )
        logger.info(
            "TUS/S3: uploaded part uid=%s part=%d size=%d offset=%d etag=%s",
            self.uid,
            part_number,
            content_length,
            offset,
            etag,
        )

        # Update annotation: chunk_size on first PATCH; last_active always.
        entry["last_active"] = time.time()
        if entry.get("chunk_size") is None:
            entry["chunk_size"] = chunk_size
        self._save_entry(entry)
        self._metadata = entry

        if is_final:
            self._complete()

    def _complete(self):
        """Verify all parts are present, call CompleteMultipartUpload."""
        entry = self._entry()
        parts = self._s3_client.list_parts(
            entry["staging_key"], entry["multipart_upload_id"]
        )
        chunk_size = entry["chunk_size"]
        length = entry["length"]
        # Expected part count: ceil(length / chunk_size).
        expected = (length + chunk_size - 1) // chunk_size
        actual_numbers = {p["PartNumber"] for p in parts}
        expected_numbers = set(range(1, expected + 1))
        missing = sorted(expected_numbers - actual_numbers)
        if missing:
            logger.error(
                "TUS/S3: cannot complete upload uid=%s — missing parts %s "
                "(expected=%d, got=%d)",
                self.uid,
                missing,
                expected,
                len(actual_numbers),
            )
            raise TUSUploadError(
                f"Cannot complete upload: missing parts {missing}",
                error_type="Conflict",
                status=409,
            )
        try:
            self._s3_client.complete_multipart_upload(
                entry["staging_key"],
                entry["multipart_upload_id"],
                parts,
            )
        except Exception:
            logger.exception(
                "TUS/S3: complete_multipart_upload failed uid=%s "
                "staging_key=%s parts=%d",
                self.uid,
                entry["staging_key"],
                len(parts),
            )
            raise
        logger.info(
            "TUS/S3: completed multipart upload uid=%s parts=%d total_bytes=%d "
            "staging_key=%s",
            self.uid,
            len(parts),
            length,
            entry["staging_key"],
        )
        self.finished = True

    def open(self):
        """Not used in S3 mode (deserializer reads via process_blob marker)."""
        raise NotImplementedError(
            "S3TUSUpload does not expose the uploaded bytes locally"
        )

    def close(self):
        """No-op — there is no local file handle in S3 mode."""

    def cleanup(self):
        """Drop annotation entry; abort multipart if upload was abandoned."""
        entry = self._entry()
        if entry:
            if (
                not self.finished
                and entry.get("multipart_upload_id")
                and self._s3_client is not None
            ):
                try:
                    self._s3_client.abort_multipart_upload(
                        entry["staging_key"], entry["multipart_upload_id"]
                    )
                except Exception:
                    logger.warning(
                        "TUS/S3: failed to abort multipart for upload %s",
                        self.uid,
                        exc_info=True,
                    )
            if (
                self.finished
                and not self._handoff_done
                and self._s3_client is not None
            ):
                # Completed S3 upload but handoff to ZODB blob never ran.
                # Leave the orphan for the lifecycle rule to clean up rather
                # than risk deleting a key the storage might end up
                # referencing.
                logger.warning(
                    "TUS/S3: upload uid=%s completed but handoff was not "
                    "performed; staging_key=%s left for lifecycle cleanup",
                    self.uid,
                    entry.get("staging_key"),
                )
            uploads = _container_uploads(self.container)
            if uploads is not None and self.uid in uploads:
                del uploads[self.uid]

    def cleanup_expired(self):
        """Remove annotation entries (and S3 multiparts) older than the period.

        Scoped to this container's annotation only — cheap and targeted, runs
        at upload-create time.
        """
        uploads = _container_uploads(self.container)
        if uploads is None:
            return
        cutoff = time.time() - self.expiration_period
        expired = [
            uid
            for uid, entry in uploads.items()
            if (entry.get("last_active") or entry.get("created") or 0) < cutoff
        ]
        for uid in expired:
            entry = uploads[uid]
            if self._s3_client is not None and entry.get("multipart_upload_id"):
                try:
                    self._s3_client.abort_multipart_upload(
                        entry["staging_key"], entry["multipart_upload_id"]
                    )
                except Exception:
                    logger.debug(
                        "TUS/S3: expired-upload abort failed (key=%s)",
                        entry.get("staging_key"),
                        exc_info=True,
                    )
            del uploads[uid]

    def process_blob(self, blob):
        """Register the staging key with the S3 storage as a blob handoff.

        Mirrors the original implementation: write a tiny diagnostic marker,
        rename it into ``_p_blob_uncommitted``, and tell the storage which
        S3 key the blob's bytes actually live at.
        """
        if self._s3_storage is None:
            raise RuntimeError(
                "S3TUSUpload missing storage reference at handoff"
            )
        size = self.length()
        entry = self._entry()
        staging_key = entry.get("staging_key")
        marker_content = (
            f"S3BLOB-STAGED\n{staging_key}\n{size}\n".encode()
        )
        # Use a uniquely-named tempfile so two concurrent uploads in the same
        # process never race on the marker source path.
        fd, marker_source = mkstemp(prefix="tus-marker-", suffix=".bin")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(marker_content)
            target_path = blob._p_blob_uncommitted
            rename_or_copy_blob(marker_source, target_path)
        finally:
            if os.path.exists(marker_source):
                os.remove(marker_source)
        self._s3_storage.register_staged_s3_key(target_path, staging_key, size)
        self._handoff_done = True
        logger.info(
            "TUS/S3: handoff complete uid=%s staging_key=%s size=%d "
            "marker_path=%s storage_id=%s",
            self.uid,
            staging_key,
            size,
            target_path,
            id(self._s3_storage),
        )


