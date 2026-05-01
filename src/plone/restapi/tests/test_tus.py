from base64 import b64encode
from DateTime import DateTime
from io import BytesIO
from OFS.interfaces import IObjectWillBeAddedEvent
from plone import api
from plone.app.testing import login
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import SITE_OWNER_PASSWORD
from plone.rest.cors import CORSPolicy
from plone.rest.interfaces import ICORSPolicy
from plone.restapi.services.content.tus import TUSUpload
from plone.restapi.testing import PLONE_RESTAPI_DX_FUNCTIONAL_TESTING
from plone.restapi.testing import RelativeSession
from zope.annotation.interfaces import IAnnotations
from zope.component import getGlobalSiteManager
from zope.component import provideAdapter
from zope.interface import Interface
from zope.lifecycleevent.interfaces import IObjectAddedEvent
from zope.lifecycleevent.interfaces import IObjectCreatedEvent
from zope.lifecycleevent.interfaces import IObjectModifiedEvent
from zope.publisher.interfaces.browser import IBrowserRequest

import os
import shutil
import tempfile
import time
import transaction
import unittest


UPLOAD_DATA = b"abcdefgh"
UPLOAD_MIMETYPE = "text/plain"
UPLOAD_FILENAME = "test.txt"
UPLOAD_LENGTH = len(UPLOAD_DATA)

UPLOAD_PDF_MIMETYPE = "application/pdf"
UPLOAD_PDF_FILENAME = "file.pdf"


def _base64_str(s):
    if not isinstance(s, bytes):
        s = s.encode("utf-8")
    s = b64encode(s)
    if not isinstance(s, str):
        s = s.decode("utf-8")
    return s


def _prepare_metadata(filename, content_type):
    return "filename {},content-type {}".format(
        _base64_str(filename), _base64_str(content_type)
    )


class TestTUS(unittest.TestCase):

    layer = PLONE_RESTAPI_DX_FUNCTIONAL_TESTING

    def setUp(self):
        self.app = self.layer["app"]
        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)

        self.folder = api.content.create(
            container=self.portal, type="Folder", id="testfolder", title="Testfolder"
        )
        self.upload_url = f"{self.folder.absolute_url()}/@tus-upload"
        transaction.commit()

        self.api_session = RelativeSession(self.portal.absolute_url(), test=self)
        self.api_session.headers.update({"Accept": "application/json"})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)

    def get_tus_uid_from_url(self, url):
        uid = url.rsplit("/", 1)[-1]
        assert len(uid) == 32
        return uid

    def get_tus_upload_instance(self, uid):
        return TUSUpload(uid)

    def test_tus_option_headers(self):
        response = self.api_session.options(self.upload_url)
        headers = response.headers
        self.assertEqual(response.status_code, 204)
        self.assertEqual(headers["Tus-Version"], "1.0.0")
        self.assertEqual(
            headers["Tus-Extension"], "creation,expiration,termination"
        )
        self.assertEqual(headers["Tus-Resumable"], "1.0.0")

    def test_tus_post_without_version_header_returns_412(self):
        response = self.api_session.post(self.upload_url)
        self.assertEqual(412, response.status_code)

    def test_tus_post_with_wrong_version_header_returns_412(self):
        response = self.api_session.post(
            self.upload_url, headers={"Tus-Resumable": "0.2.2"}
        )
        self.assertEqual(412, response.status_code)

    def test_tus_post_initialization_requires_header_length(self):
        response = self.api_session.post(
            self.upload_url, headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(response.json()["error"]["type"], "Bad Request")
        self.assertEqual(
            response.json()["error"]["message"],
            "Missing or invalid Upload-Length header",
        )
        self.assertEqual(response.status_code, 400)

    def test_tus_post_initialization(self):
        response = self.api_session.post(
            self.upload_url,
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(UPLOAD_LENGTH)},
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]
        url_base, uid = location.rsplit("/", 1)
        self.assertEqual(url_base, self.upload_url)
        self.assertEqual(len(uid), 32)
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(stored_metadata, {"length": 8, "mode": "create"})
        upload.cleanup()

    def test_tus_post_initialization_with_metadata(self):
        metadata = _prepare_metadata(UPLOAD_FILENAME, UPLOAD_MIMETYPE)
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(UPLOAD_LENGTH),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        uid = self.get_tus_uid_from_url(response.headers["Location"])
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(
            stored_metadata,
            {
                "content-type": "text/plain",
                "filename": "test.txt",
                "length": 8,
                "mode": "create",
            },
        )
        upload.cleanup()

    def test_tus_post_replace(self):
        self.file = api.content.create(
            container=self.portal, type="File", id="testfile", title="Testfile"
        )
        transaction.commit()
        response = self.api_session.post(
            f"{self.file.absolute_url()}/@tus-replace",
            headers={"Tus-Resumable": "1.0.0", "Upload-Length": str(UPLOAD_LENGTH)},
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]
        url_base, uid = location.rsplit("/", 1)
        upload = TUSUpload(uid)
        stored_metadata = upload.metadata()
        self.assertEqual(stored_metadata, {"length": 8, "mode": "replace"})
        upload.cleanup()

    def test_tus_head_on_not_existing_resource_returns_404(self):
        response = self.api_session.head(
            self.upload_url + "/myuid/123", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)
        response = self.api_session.head(
            self.upload_url + "/non-existing-uid", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)
        response = self.api_session.head(
            self.upload_url, headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)

    def test_tus_head_with_unsupported_version_returns_412(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.head(
            self.upload_url + "/myuid", headers={"Tus-Resumable": "0.2.2"}
        )
        self.assertEqual(412, response.status_code)
        tus.cleanup()

    def test_tus_head_response_includes_required_headers(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.head(
            self.upload_url + "/myuid", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertIn("Upload-Length", response.headers)
        self.assertEqual("2048", response.headers["Upload-Length"])
        self.assertIn("Upload-Offset", response.headers)
        self.assertIn("Tus-Resumable", response.headers)
        self.assertIn("Cache-Control", response.headers)
        tus.cleanup()

    def test_head_in_create_mode_without_add_permission_raises_401(self):
        self.folder.manage_permission("Add portal content", [], 0)
        transaction.commit()
        tus = TUSUpload("myuid", {"mode": "create", "length": 12})
        response = self.api_session.head(
            self.upload_url + "/myuid",
            headers={"Tus-Resumable": "1.0.0", "Upload-Offset": "0"},
        )
        self.assertEqual(401, response.status_code)
        tus.cleanup()

    def test_head_in_replace_mode_without_modify_permission_raises_401(self):
        self.folder.manage_permission("Modify portal content", [], 0)
        transaction.commit()
        tus = TUSUpload("myuid", {"mode": "replace", "length": 12})
        response = self.api_session.head(
            self.upload_url + "/myuid",
            headers={"Tus-Resumable": "1.0.0", "Upload-Offset": "0"},
        )
        self.assertEqual(401, response.status_code)
        tus.cleanup()

    def test_tus_patch_on_not_existing_resource_returns_404(self):
        response = self.api_session.patch(
            self.upload_url + "/myuid/123", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)
        response = self.api_session.patch(
            self.upload_url + "/myuid", headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)
        response = self.api_session.patch(
            self.upload_url, headers={"Tus-Resumable": "1.0.0"}
        )
        self.assertEqual(404, response.status_code)

    def test_tus_patch_with_unsupported_version_returns_412(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.patch(
            self.upload_url + "/myuid", headers={"Tus-Resumable": "0.2.2"}
        )
        self.assertEqual(412, response.status_code)
        tus.cleanup()

    def test_tus_patch_with_unsupported_content_type_returns_400(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={"Tus-Resumable": "1.0.0", "Content-Type": "application/json"},
        )
        self.assertEqual(400, response.status_code)
        tus.cleanup()

    def test_tus_patch_with_invalid_offset_returns_400(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
            },
        )
        self.assertEqual(400, response.status_code)
        tus.cleanup()

    def test_tus_patch_unfinished_upload_returns_expires_header(self):
        tus = TUSUpload("myuid", {"length": 2048})
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
            data=BytesIO(b"abcdefghijkl"),
        )
        self.assertEqual(204, response.status_code)
        self.assertIn("Upload-Expires", response.headers)
        tus.cleanup()

    def test_tus_patch_non_primary_field(self):
        tus = TUSUpload(
            "myuid",
            {
                "@type": "DXTestDocument",
                "length": 12,
                "fieldname": "test_namedblobfile_field",
            },
        )
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
            data=BytesIO(b"abcdefghijkl"),
        )

        self.assertEqual(204, response.status_code)
        transaction.commit()
        self.assertEqual(1, len(self.folder.objectIds()))
        id_ = self.folder.objectIds()[0]
        self.assertEqual(
            b"abcdefghijkl", self.folder[id_].test_namedblobfile_field.data
        )
        tus.cleanup()

    def test_patch_in_create_mode_without_add_permission_raises_401(self):
        self.folder.manage_permission("Add portal content", [], 0)
        transaction.commit()
        tus = TUSUpload("myuid", {"mode": "create", "length": 12})
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
            data=BytesIO(b"abcdefghijkl"),
        )
        self.assertEqual(401, response.status_code)
        tus.cleanup()

    def test_patch_in_replace_mode_without_modify_permission_raises_401(self):
        self.folder.manage_permission("Modify portal content", [], 0)
        transaction.commit()
        tus = TUSUpload("myuid", {"mode": "replace", "length": 12})
        response = self.api_session.patch(
            self.upload_url + "/myuid",
            headers={
                "Tus-Resumable": "1.0.0",
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
            },
            data=BytesIO(b"abcdefghijkl"),
        )
        self.assertEqual(401, response.status_code)
        tus.cleanup()

    def test_tus_can_upload_pdf_file(self):
        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__), UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = _prepare_metadata(UPLOAD_PDF_FILENAME, UPLOAD_PDF_MIMETYPE)
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(pdf_file_size),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]

        # upload the data with PATCH
        with open(pdf_file_path, "rb") as pdf_file:
            response = self.api_session.patch(
                location,
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Tus-Resumable": "1.0.0",
                },
                data=pdf_file,
            )
        self.assertEqual(response.status_code, 204)

        transaction.commit()
        self.assertEqual([UPLOAD_PDF_FILENAME], self.folder.contentIds())

    def test_tus_can_upload_text_file(self):
        # initialize the upload with POST
        metadata = _prepare_metadata(UPLOAD_FILENAME, UPLOAD_MIMETYPE)
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(UPLOAD_LENGTH),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]

        # upload the data with PATCH
        response = self.api_session.patch(
            location,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
                "Tus-Resumable": "1.0.0",
            },
            data=BytesIO(UPLOAD_DATA),
        )
        self.assertEqual(response.status_code, 204)

    def test_tus_can_replace_pdf_file(self):
        # Create a test file
        self.file = api.content.create(
            container=self.portal, type="File", id="testfile", title="Testfile"
        )
        transaction.commit()
        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__), UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = _prepare_metadata(UPLOAD_PDF_FILENAME, UPLOAD_PDF_MIMETYPE)
        response = self.api_session.post(
            f"{self.file.absolute_url()}/@tus-replace",
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(pdf_file_size),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]

        # upload the data with PATCH
        with open(pdf_file_path, "rb") as pdf_file:
            response = self.api_session.patch(
                location,
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Tus-Resumable": "1.0.0",
                },
                data=pdf_file,
            )
        self.assertEqual(response.status_code, 204)

        transaction.commit()
        self.assertEqual(UPLOAD_PDF_FILENAME, self.file.file.filename)
        self.assertEqual(pdf_file_size, self.file.file.size)

    def test_create_with_tus_fires_proper_events(self):
        sm = getGlobalSiteManager()
        fired_events = []

        def record_event(event):
            fired_events.append(event.__class__.__name__)

        sm.registerHandler(record_event, (IObjectCreatedEvent,))
        sm.registerHandler(record_event, (IObjectWillBeAddedEvent,))
        sm.registerHandler(record_event, (IObjectAddedEvent,))
        sm.registerHandler(record_event, (IObjectModifiedEvent,))

        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__), UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = _prepare_metadata(UPLOAD_PDF_FILENAME, UPLOAD_PDF_MIMETYPE)
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(pdf_file_size),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]

        # upload the data with PATCH
        with open(pdf_file_path, "rb") as pdf_file:
            response = self.api_session.patch(
                location,
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Tus-Resumable": "1.0.0",
                },
                data=pdf_file,
            )
        self.assertEqual(response.status_code, 204)

        self.assertEqual(
            fired_events,
            [
                "ObjectCreatedEvent",
                "ObjectWillBeAddedEvent",
                "ObjectAddedEvent",
                "ContainerModifiedEvent",
            ],
        )

        sm.unregisterHandler(record_event, (IObjectCreatedEvent,))
        sm.unregisterHandler(record_event, (IObjectWillBeAddedEvent,))
        sm.unregisterHandler(record_event, (IObjectAddedEvent,))
        sm.unregisterHandler(record_event, (IObjectModifiedEvent,))

    def test_replace_with_tus_fires_proper_events(self):
        # Create a test file
        self.file = api.content.create(
            container=self.portal, type="File", id="testfile", title="Testfile"
        )
        transaction.commit()

        sm = getGlobalSiteManager()
        fired_events = []

        def record_event(event):
            fired_events.append(event.__class__.__name__)

        sm.registerHandler(record_event, (IObjectCreatedEvent,))
        sm.registerHandler(record_event, (IObjectWillBeAddedEvent,))
        sm.registerHandler(record_event, (IObjectAddedEvent,))
        sm.registerHandler(record_event, (IObjectModifiedEvent,))

        # initialize the upload with POST
        pdf_file_path = os.path.join(os.path.dirname(__file__), UPLOAD_PDF_FILENAME)
        pdf_file_size = os.path.getsize(pdf_file_path)
        metadata = _prepare_metadata(UPLOAD_PDF_FILENAME, UPLOAD_PDF_MIMETYPE)
        response = self.api_session.post(
            f"{self.file.absolute_url()}/@tus-replace",
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(pdf_file_size),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]

        # upload the data with PATCH
        with open(pdf_file_path, "rb") as pdf_file:
            response = self.api_session.patch(
                location,
                headers={
                    "Content-Type": "application/offset+octet-stream",
                    "Upload-Offset": "0",
                    "Tus-Resumable": "1.0.0",
                },
                data=pdf_file,
            )
        self.assertEqual(response.status_code, 204)

        self.assertEqual(fired_events, ["ObjectModifiedEvent"])

        sm.unregisterHandler(record_event, (IObjectCreatedEvent,))
        sm.unregisterHandler(record_event, (IObjectWillBeAddedEvent,))
        sm.unregisterHandler(record_event, (IObjectAddedEvent,))
        sm.unregisterHandler(record_event, (IObjectModifiedEvent,))

    def test_tus_with_api(self):
        self.upload_url = (
            f"{self.portal.absolute_url()}/++api++/{self.folder.id}/@tus-upload"
        )
        metadata = _prepare_metadata(UPLOAD_FILENAME, UPLOAD_MIMETYPE)
        response = self.api_session.post(
            self.upload_url,
            headers={
                "Tus-Resumable": "1.0.0",
                "Upload-Length": str(UPLOAD_LENGTH),
                "Upload-Metadata": metadata,
            },
        )
        self.assertEqual(response.status_code, 201)
        location = response.headers["Location"]
        self.assertEqual("++api++" in location, True)

        # upload the data with PATCH
        response = self.api_session.patch(
            location,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": "0",
                "Tus-Resumable": "1.0.0",
            },
            data=BytesIO(UPLOAD_DATA),
        )
        self.assertEqual(response.status_code, 204)

    def tearDown(self):
        self.api_session.close()
        client_home = os.environ.get("CLIENT_HOME")
        tmp_dir = os.path.join(client_home, "tus-uploads")
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)


class CORSTestPolicy(CORSPolicy):
    allow_origin = ["*"]
    allow_methods = ["DELETE", "GET", "OPTIONS", "PATCH", "POST", "PUT"]
    allow_credentials = True
    allow_headers = [
        "Accept",
        "Authorization",
        "Origin",
        "X-Requested-With",
        "Content-Type",
        "Tus-Resumable",
        "Upload-Length",
        "Upload-Offset",
    ]
    expose_header = []
    max_age = 3600


class TestTUSUploadWithCORS(unittest.TestCase):

    layer = PLONE_RESTAPI_DX_FUNCTIONAL_TESTING

    def setUp(self):
        provideAdapter(
            CORSTestPolicy, adapts=(Interface, IBrowserRequest), provides=ICORSPolicy
        )
        self.portal = self.layer["portal"]
        self.api_session = RelativeSession(self.portal.absolute_url(), test=self)
        self.api_session.headers.update({"Accept": "application/json"})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)
        self.upload_url = f"{self.portal.absolute_url()}/@tus-upload"

    def test_cors_preflight_for_post_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url,
            headers={
                "Origin": "http://myhost.net",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Tus-Resumable,Upload-Length",
            },
        )
        self.assertIn("Tus-Resumable", response.headers)
        self.assertIn("Tus-Version", response.headers)
        self.assertIn("Tus-Extension", response.headers)

    def test_cors_preflight_for_patch_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url,
            headers={
                "Origin": "http://myhost.net",
                "Access-Control-Request-Method": "PATCH",
                "Access-Control-Request-Headers": "Content-Type,Tus-Resumable,Upload-Offset",
            },
        )
        self.assertIn("Tus-Resumable", response.headers)
        self.assertIn("Tus-Version", response.headers)
        self.assertIn("Tus-Extension", response.headers)

    def test_cors_preflight_for_head_contains_tus_headers(self):
        response = self.api_session.options(
            self.upload_url,
            headers={
                "Origin": "http://myhost.net",
                "Access-Control-Request-Method": "HEAD",
                "Access-Control-Request-Headers": "Tus-Resumable",
            },
        )
        self.assertIn("Tus-Resumable", response.headers)
        self.assertIn("Tus-Version", response.headers)
        self.assertIn("Tus-Extension", response.headers)

    def tearDown(self):
        self.api_session.close()
        gsm = getGlobalSiteManager()
        gsm.unregisterAdapter(CORSTestPolicy, (Interface, IBrowserRequest), ICORSPolicy)


class TestTUSUpload(unittest.TestCase):
    def test_tmp_dir_gets_created_in_client_home(self):
        tus = TUSUpload("myuid")
        self.assertTrue(os.path.isdir(tus.tmp_dir))
        tus.cleanup()

    def test_use_tus_tmp_dir_if_provided(self):
        tus_upload_dir = tempfile.mkdtemp()
        os.environ["TUS_TMP_FILE_DIR"] = tus_upload_dir
        tus = TUSUpload("myuid")
        self.assertEqual(tus_upload_dir, tus.tmp_dir)
        tus.cleanup()

    def test_metadata_gets_stored_if_provided(self):
        tus = TUSUpload("myuid", {"length": 1024, "filename": "test.pdf"})
        self.assertIn("filename", tus.metadata())
        self.assertEqual("test.pdf", tus.metadata()["filename"])
        tus.cleanup()

    def test_length_returns_total_length_if_set(self):
        tus = TUSUpload("myuid", {"length": 1024})
        self.assertEqual(1024, tus.length())
        tus.cleanup()

    def test_length_returns_zero_if_not_set(self):
        tus = TUSUpload("myuid")
        self.assertEqual(0, tus.length())
        tus.cleanup()

    def test_offset_returns_zero_if_file_doesnt_exist(self):
        tus = TUSUpload("myuid", {"length": 1024})
        self.assertEqual(0, tus.offset())
        tus.cleanup()

    def test_offset_returns_size_of_current_file(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.write(BytesIO(b"0123456789"))
        self.assertEqual(10, tus.offset())
        tus.cleanup()

    def test_write_creates_new_file(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.write(BytesIO(b"0123456789"))
        self.assertTrue(os.path.isfile(tus.filepath))
        tus.cleanup()

    def test_write_appends_to_file_at_given_offset(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.write(BytesIO(b"0123456789"))
        tus.write(BytesIO(b"abc"), 10)
        self.assertEqual(13, tus.offset())
        with open(tus.filepath, "rb") as f:
            data = f.read()
        self.assertEqual(b"0123456789abc", data)
        tus.cleanup()

    def test_write_sets_finished_flag(self):
        tus = TUSUpload("myuid", {"length": 10})
        tus.write(BytesIO(b"0123456789"))
        self.assertTrue(tus.finished)
        tus.cleanup()

    def test_metadata_returns_empty_dict_if_no_metadata_has_been_set(self):
        tus = TUSUpload("myuid")
        self.assertEqual({}, tus.metadata())
        tus.cleanup()

    def test_expires_returns_expiration_time_of_current_upload(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.write(BytesIO(b"0123456789"))
        self.assertGreater(DateTime(tus.expires()), DateTime())
        tus.cleanup()

    def test_cleanup_removes_upload_file(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.write(BytesIO(b"0123456789"))
        tus.cleanup()
        self.assertFalse(os.path.exists(tus.filepath))

    def test_cleanup_removes_metadata_file(self):
        tus = TUSUpload("myuid", {"length": 1024})
        tus.cleanup()
        self.assertFalse(os.path.exists(tus.metadata_path))

    def test_cleanup_expired_files(self):
        tus = TUSUpload("myuid")
        filepath = os.path.join(tus.tmp_dir, "tus_upload_12345")
        metadata_path = os.path.join(tus.tmp_dir, "tus_upload_12345.json")
        metadata_only_path = os.path.join(tus.tmp_dir, "tus_upload_67890.json")
        open(filepath, "wb").close()
        os.utime(filepath, (946684800.0, 946684800.0))
        open(metadata_path, "wb").close()
        os.utime(metadata_path, (946684800.0, 946684800.0))
        open(metadata_only_path, "wb").close()
        os.utime(metadata_only_path, (946684800.0, 946684800.0))
        tus.cleanup_expired()
        self.assertFalse(os.path.exists(filepath))
        self.assertFalse(os.path.exists(metadata_path))
        self.assertFalse(os.path.exists(metadata_only_path))
        tus.cleanup()

    def tearDown(self):
        client_home = os.environ.get("CLIENT_HOME")
        tmp_dir = os.path.join(client_home, "tus-uploads")
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)


class FakeS3Client:
    """In-memory stand-in for ``zodb_s3blobs.s3client.S3Client``.

    Records the parts uploaded for each (key, upload_id) and tracks the
    multipart upload lifecycle (created / completed / aborted). Just enough
    surface to exercise ``S3TUSUpload``.
    """

    def __init__(self, bucket_name="test-bucket"):
        self.bucket_name = bucket_name
        # {key: upload_id}
        self._uploads = {}
        # {(key, upload_id): {part_number: {'ETag': ..., 'Size': ...}}}
        self._parts = {}
        self._completed = set()
        self._aborted = set()
        self._next_upload_id = 1
        # Lifecycle state: in-memory rules list + counters for assertions.
        self._lifecycle_rules = []
        self._lifecycle_get_calls = 0
        self._lifecycle_put_calls = 0
        # Configurable error injection for tests.
        self._lifecycle_get_raises = None
        self._lifecycle_put_raises = None

    def ensure_abort_multipart_lifecycle_rule(self, rule_id, prefix, days=7):
        self._lifecycle_get_calls += 1
        if self._lifecycle_get_raises is not None:
            raise self._lifecycle_get_raises
        if any(r.get("ID") == rule_id for r in self._lifecycle_rules):
            return True
        self._lifecycle_put_calls += 1
        if self._lifecycle_put_raises is not None:
            raise self._lifecycle_put_raises
        self._lifecycle_rules.append(
            {
                "ID": rule_id,
                "Status": "Enabled",
                "Filter": {"Prefix": prefix},
                "AbortIncompleteMultipartUpload": {
                    "DaysAfterInitiation": days
                },
                "Expiration": {"Days": days},
            }
        )
        return True

    def create_multipart_upload(self, s3_key):
        upload_id = f"mpu-{self._next_upload_id}"
        self._next_upload_id += 1
        self._uploads[s3_key] = upload_id
        self._parts[(s3_key, upload_id)] = {}
        return upload_id

    def upload_part(self, s3_key, upload_id, part_number, body):
        # Drain the body to simulate boto3 reading it.
        data = body.read() if hasattr(body, "read") else body
        size = len(data)
        etag = f'"etag-{s3_key}-{part_number}"'
        self._parts[(s3_key, upload_id)][part_number] = {
            "ETag": etag,
            "Size": size,
            "Data": data,
        }
        return etag

    def list_parts(self, s3_key, upload_id):
        parts = self._parts.get((s3_key, upload_id), {})
        return [
            {"PartNumber": n, "ETag": p["ETag"], "Size": p["Size"]}
            for n, p in sorted(parts.items())
        ]

    def complete_multipart_upload(self, s3_key, upload_id, parts):
        self._completed.add((s3_key, upload_id))

    def abort_multipart_upload(self, s3_key, upload_id):
        self._aborted.add((s3_key, upload_id))


class FakeS3Storage:
    """Minimal stand-in for S3BlobStorage.register_staged_s3_key."""

    def __init__(self):
        self.registered = []

    def register_staged_s3_key(self, blob_path, staging_key, size):
        self.registered.append((blob_path, staging_key, size))


class TestS3TUSUpload(unittest.TestCase):
    """Unit tests for the annotation-backed S3 TUS path."""

    layer = PLONE_RESTAPI_DX_FUNCTIONAL_TESTING

    def setUp(self):
        from plone import api as plone_api

        self.app = self.layer["app"]
        self.portal = self.layer["portal"]
        login(self.portal, SITE_OWNER_NAME)
        self.folder = plone_api.content.create(
            container=self.portal,
            type="Folder",
            id="s3tustestfolder",
            title="S3 TUS Test Folder",
        )
        transaction.commit()
        self.s3 = FakeS3Client()
        self.storage = FakeS3Storage()

    def _make_upload(self, uid="abc123", length=30 * 1024 * 1024, **extra):
        from plone.restapi.services.content.tus import S3TUSUpload

        meta = {
            "length": length,
            "filename": "test.bin",
            "content-type": "application/octet-stream",
            "@type": "File",
            "mode": "create",
        }
        meta.update(extra)
        return S3TUSUpload(
            uid,
            container=self.folder,
            metadata=meta,
            s3_storage=self.storage,
            s3_client=self.s3,
        )

    def test_initialization_creates_multipart_and_writes_annotation(self):
        from plone.restapi.services.content.tus import (
            ANNOTATION_KEY,
            _container_uploads,
        )

        tus = self._make_upload(uid="uid-1")
        # Multipart created with deterministic staging key
        self.assertEqual(self.s3._uploads, {"tus-staging/uid-1": "mpu-1"})
        # Annotation written
        uploads = _container_uploads(self.folder)
        self.assertIsNotNone(uploads)
        self.assertIn("uid-1", uploads)
        entry = uploads["uid-1"]
        self.assertEqual(entry["staging_key"], "tus-staging/uid-1")
        self.assertEqual(entry["multipart_upload_id"], "mpu-1")
        self.assertEqual(entry["length"], 30 * 1024 * 1024)
        self.assertIsNone(entry["chunk_size"])
        # Sanity: ANNOTATION_KEY is the right place
        self.assertIn(ANNOTATION_KEY, IAnnotations(self.folder))

    def test_metadata_and_length_read_from_annotation(self):
        tus = self._make_upload(filename="report.pdf", length=12345)
        self.assertEqual(tus.length(), 12345)
        self.assertEqual(tus.metadata()["filename"], "report.pdf")

    def test_offset_zero_until_first_chunk_uploaded(self):
        tus = self._make_upload()
        self.assertEqual(tus.offset(), 0)

    def test_offset_uses_contiguous_prefix_of_parts(self):
        # 30 MiB total, 10 MiB chunks. Upload parts 1, 2, and 4 — gap at 3.
        # Contiguous offset = 2 * chunk_size.
        chunk = 10 * 1024 * 1024
        tus = self._make_upload(length=4 * chunk)
        # Manually inject parts since we don't have a real request body here.
        key = "tus-staging/abc123"
        upload_id = "mpu-1"
        for part_number in (1, 2, 4):
            self.s3._parts[(key, upload_id)][part_number] = {
                "ETag": f'"e-{part_number}"',
                "Size": chunk,
                "Data": b"",
            }
        # Set chunk_size in the annotation so offset() uses it.
        from plone.restapi.services.content.tus import _container_uploads

        entry = _container_uploads(self.folder)["abc123"]
        entry["chunk_size"] = chunk
        _container_uploads(self.folder)["abc123"] = entry
        self.assertEqual(tus.offset(), 2 * chunk)

    def test_write_uses_deterministic_part_number(self):
        chunk = 10 * 1024 * 1024
        length = 3 * chunk
        tus = self._make_upload(uid="dpn", length=length)
        # First chunk at offset 0.
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Second chunk at offset chunk.
        tus.write(BytesIO(b"y" * chunk), offset=chunk, content_length=chunk)
        parts = self.s3._parts[("tus-staging/dpn", "mpu-1")]
        self.assertEqual(sorted(parts.keys()), [1, 2])
        self.assertEqual(parts[1]["Size"], chunk)
        self.assertEqual(parts[2]["Size"], chunk)

    def test_write_first_patch_learns_chunk_size(self):
        from plone.restapi.services.content.tus import _container_uploads

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="learn", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        entry = _container_uploads(self.folder)["learn"]
        self.assertEqual(entry["chunk_size"], chunk)

    def test_write_rejects_non_final_chunk_below_5mib(self):
        from plone.restapi.services.content.tus import TUSUploadError

        tus = self._make_upload(uid="small", length=100 * 1024 * 1024)
        small = 4 * 1024 * 1024  # below 5 MiB
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(BytesIO(b"x" * small), offset=0, content_length=small)
        self.assertIn("5 MiB", str(ctx.exception))

    def test_write_rejects_offset_not_multiple_of_chunk_size(self):
        from plone.restapi.services.content.tus import TUSUploadError

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="badoff", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Now send at a misaligned offset.
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(
                BytesIO(b"y" * chunk),
                offset=chunk + 1,
                content_length=chunk,
            )
        self.assertIn("not a multiple", str(ctx.exception))

    def test_write_rejects_size_mismatch_for_non_final_chunk(self):
        from plone.restapi.services.content.tus import TUSUploadError

        chunk = 10 * 1024 * 1024
        tus = self._make_upload(uid="badsize", length=3 * chunk)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        # Subsequent non-final PATCH must equal chunk_size.
        bad = chunk - 100
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(BytesIO(b"y" * bad), offset=chunk, content_length=bad)
        self.assertIn("does not match", str(ctx.exception))

    def test_final_chunk_completes_multipart(self):
        chunk = 10 * 1024 * 1024
        length = 2 * chunk + 100  # final part is small (100 bytes)
        tus = self._make_upload(uid="finish", length=length)
        tus.write(BytesIO(b"a" * chunk), offset=0, content_length=chunk)
        tus.write(BytesIO(b"b" * chunk), offset=chunk, content_length=chunk)
        # Final small chunk
        tus.write(BytesIO(b"c" * 100), offset=2 * chunk, content_length=100)
        self.assertTrue(tus.finished)
        self.assertIn(
            ("tus-staging/finish", "mpu-1"), self.s3._completed
        )

    def test_complete_with_missing_parts_raises_409(self):
        from plone.restapi.services.content.tus import TUSUploadError

        chunk = 10 * 1024 * 1024
        length = 3 * chunk
        tus = self._make_upload(uid="gap", length=length)
        tus.write(BytesIO(b"a" * chunk), offset=0, content_length=chunk)
        # Skip middle chunk; pretend client thinks last chunk completes
        # (length boundary). The write at offset=2*chunk would set is_final.
        with self.assertRaises(TUSUploadError) as ctx:
            tus.write(
                BytesIO(b"c" * chunk),
                offset=2 * chunk,
                content_length=chunk,
            )
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("missing parts", str(ctx.exception))

    def test_cleanup_unfinished_aborts_multipart_and_drops_entry(self):
        from plone.restapi.services.content.tus import _container_uploads

        tus = self._make_upload(uid="abort")
        tus.cleanup()
        self.assertIn(
            ("tus-staging/abort", "mpu-1"), self.s3._aborted
        )
        self.assertNotIn("abort", _container_uploads(self.folder))

    def test_cleanup_finished_does_not_abort(self):
        chunk = 10 * 1024 * 1024
        length = chunk
        tus = self._make_upload(uid="ok", length=length)
        tus.write(BytesIO(b"x" * chunk), offset=0, content_length=chunk)
        self.assertTrue(tus.finished)
        # Simulate handoff completing before cleanup
        tus._handoff_done = True
        tus.cleanup()
        self.assertNotIn(
            ("tus-staging/ok", "mpu-1"), self.s3._aborted
        )

    def test_cleanup_expired_removes_old_entries(self):
        from plone.restapi.services.content.tus import _container_uploads

        tus_old = self._make_upload(uid="old")
        tus_new = self._make_upload(uid="new")
        # Backdate "old" past the expiration period.
        uploads = _container_uploads(self.folder)
        uploads["old"]["last_active"] = (
            time.time() - tus_old.expiration_period - 1
        )
        # Triggering cleanup_expired through any S3TUSUpload instance.
        tus_new.cleanup_expired()
        self.assertNotIn("old", _container_uploads(self.folder))
        self.assertIn("new", _container_uploads(self.folder))
        self.assertIn(
            ("tus-staging/old", "mpu-1"), self.s3._aborted
        )

    def tearDown(self):
        from plone.restapi.services.content.tus import (
            ANNOTATION_KEY,
        )

        ann = IAnnotations(self.folder)
        if ANNOTATION_KEY in ann:
            del ann[ANNOTATION_KEY]
        # Tear the folder down so the next test starts clean.
        api.content.delete(obj=self.folder)
        transaction.commit()


class TestTUSLifecycleAutoApply(unittest.TestCase):
    """Tests for the once-per-process lifecycle rule auto-apply."""

    def setUp(self):
        # Reset the process-local set between tests.
        from plone.restapi.services.content.tus import _LIFECYCLE_APPLIED

        _LIFECYCLE_APPLIED.clear()
        self.s3 = FakeS3Client(bucket_name="bucket-a")

    def test_first_call_applies_and_marks_bucket(self):
        from plone.restapi.services.content.tus import (
            _ensure_lifecycle_rule_once,
            _LIFECYCLE_APPLIED,
        )

        _ensure_lifecycle_rule_once(self.s3)
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(self.s3._lifecycle_put_calls, 1)
        self.assertIn("bucket-a", _LIFECYCLE_APPLIED)

    def test_second_call_is_a_noop(self):
        from plone.restapi.services.content.tus import (
            _ensure_lifecycle_rule_once,
        )

        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(self.s3)
        # Still only one round trip — the second call is short-circuited
        # by the process-local set.
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(self.s3._lifecycle_put_calls, 1)

    def test_failure_is_swallowed_and_bucket_still_marked(self):
        from plone.restapi.services.content.tus import (
            _ensure_lifecycle_rule_once,
            _LIFECYCLE_APPLIED,
        )

        self.s3._lifecycle_get_raises = RuntimeError("boom")
        # Should not raise; should still record the bucket so subsequent
        # POSTs don't keep retrying.
        _ensure_lifecycle_rule_once(self.s3)
        self.assertIn("bucket-a", _LIFECYCLE_APPLIED)

    def test_separate_buckets_are_each_handled_once(self):
        from plone.restapi.services.content.tus import (
            _ensure_lifecycle_rule_once,
        )

        s3_b = FakeS3Client(bucket_name="bucket-b")
        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(s3_b)
        _ensure_lifecycle_rule_once(self.s3)
        _ensure_lifecycle_rule_once(s3_b)
        self.assertEqual(self.s3._lifecycle_get_calls, 1)
        self.assertEqual(s3_b._lifecycle_get_calls, 1)
