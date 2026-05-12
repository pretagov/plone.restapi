---
myst:
  html_meta:
    "description": "plone.restapi supports the TUS Open Protocol for resumable file uploads. There is a @tus-upload endpoint to upload a file, and a @tus-replace endpoint to replace an existing file."
    "property=og:description": "plone.restapi supports the TUS Open Protocol for resumable file uploads. There is a @tus-upload endpoint to upload a file, and a @tus-replace endpoint to replace an existing file."
    "property=og:title": "TUS resumable upload"
    "keywords": "Plone, plone.restapi, REST, API, TUS, resumable, upload"
---

# TUS resumable upload

`plone.restapi` supports the [TUS Open Protocol](https://tus.io/) for resumable file uploads.
There is a `@tus-upload` endpoint to upload a file, and a `@tus-replace` endpoint to replace an existing file.


## Creating an Upload URL

```{note}
`POST` requests to the `@tus-upload` endpoint are allowed on all `IFolderish` content types, for example, `Folder`.
```

To create a new upload, send a `POST` request to the `@tus-upload` endpoint:

```{eval-rst}
..  http:example:: curl httpie python-requests
    :request: ../../../src/plone/restapi/tests/http-examples/tusupload_post.req
```

The server will return a temporary upload URL in the `Location` header of the response:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_post.resp
:language: http
```

The file can then be uploaded in the next step to that temporary URL.


## Uploading a File

```{note}
PATCH requests to the `@tus-upload` endpoint are allowed on all IContentish content types.
```

Once a temporary upload URL has been created, a client can send a `PATCH` request to upload a file.
The file content should be sent in the body of the request:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_patch_finalized.req
:language: http
```

When just a single file is uploaded at once, the server will respond with a {term}`204 No Content` response after a successful upload.
The HTTP `Location` header contains he URL of the newly created content object:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_patch_finalized.resp
:language: http
```


## Partial Upload

TUS allows partial upload of files.
A partial file is also uploaded by sending a `PATCH` request to the temporary URL:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_patch.req
:language: http
```

The server will also respond with a {term}`204 No content` response.
Though instead of providing the final file URL in the `Location` header, the server provides an updated `Upload-Offset` value, telling the client the new offset:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_patch.resp
:language: http
```

When the last partial file has been uploaded, the server will contain the final file URL in the `Location` header.

## Replacing Existing Files

TUS can also be used to replace an existing file by sending a `POST` request to the `@tus-replace` endpoint instead:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusreplace_post.req
:language: http
```

The server will respond with a {term}`201 Created` status and return the URL of the temporarily created upload resource in the `Location` header of the response:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_post.resp
:language: http
```

The file can then be uploaded to that URL using the `PATCH` method in the same way as creating a new file:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusreplace_patch.req
:language: http
```

The server will respond with a {term}`204 No Content` response and the final file URL in the HTTP `Location` header:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusreplace_patch.resp
:language: http
```


## Asking for the Current File Offset

To ask the server for the current file offset, the client can send a `HEAD` request to the upload URL:

```{eval-rst}
..  http:example:: curl httpie python-requests
    :request: ../../../src/plone/restapi/tests/http-examples/tusupload_head.req
```

The server will respond with a {term}`200 OK` status and the current file offset in the `Upload-Offset` header:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_head.resp
:language: http
```


## Configuration and Options

The current TUS configuration and a list of supported options can be retrieved sending an `OPTIONS` request to the `@tus-upload` endpoint:

```{eval-rst}
..  http:example:: curl httpie python-requests
    :request: ../../../src/plone/restapi/tests/http-examples/tusupload_options.req
```

The server will respond with a {term}`204 No content` status and HTTP headers containing information about the available extensions and the TUS version:

```{literalinclude} ../../../src/plone/restapi/tests/http-examples/tusupload_options.resp
:language: http
```


## CORS Configuration

If you use CORS and want to make it work with TUS, you have to make sure the TUS-specific HTTP headers are allowed by your CORS policy:

```xml
<plone:CORSPolicy
  allow_origin="http://localhost"
  allow_methods="DELETE,GET,OPTIONS,PATCH,POST,PUT"
  allow_credentials="true"
  allow_headers="Accept,Authorization,Origin,X-Requested-With,Content-Type,Upload-Length,Upload-Offset,Tus-Resumable,Upload-Metadata,Lock-Token"
  expose_headers="Upload-Offset,Location,Upload-Length,Tus-Version,Tus-Resumable,Tus-Max-Size,Tus-Extension,Upload-Metadata"
  max_age="3600"
  />
```

See the `plone.rest` documentation for more information on how to configure CORS policies.

See <https://tus.io/protocols/resumable-upload.html#headers> for a list and description of the individual headers.


## Temporary Upload Directory (local-disk mode)

When the ZODB storage is **not** S3-backed, in-progress uploads are buffered to a temporary directory that defaults to `CLIENT_HOME/tus-uploads`.
If you are using a multi-ZEO-client setup without session stickiness in this mode you *must* configure this to a directory shared by all clients by setting the `TUS_TMP_FILE_DIR` environment variable, for example `TUS_TMP_FILE_DIR=/tmp/tus-uploads`.

This directory is unused when the storage is S3-backed (see below).


## S3-backed storage

When the ZODB storage is `zodb_s3blobs.S3BlobStorage`, the TUS service streams chunks directly into an S3 multipart upload under the `tus-staging/` key prefix.
State for in-progress uploads is held in a ZODB annotation on the upload's container (key `plone.restapi.tus_uploads`), which makes the data path independent of which worker handled which chunk — any worker can serve any `PATCH`, `HEAD`, or `DELETE` for an upload, so sticky-routing is unnecessary.

A successful final `PATCH` triggers `CompleteMultipartUpload` and hands the staging key off to `S3BlobStorage` for registration; the annotation entry is removed at that point.
Abandoned uploads (browser tab closed mid-chunk, network drop, worker crash) leave dangling multiparts behind. S3 charges for storage of in-progress parts and silently retains them until they're explicitly aborted — so deployments using S3-backed storage should configure a lifecycle rule that auto-aborts stale staging multiparts.

### Lifecycle rule for abandoned multipart uploads

The TUS service tries to install the rule on the first `POST` per process via `S3Client.ensure_abort_multipart_lifecycle_rule`.
The call is idempotent (it reads the bucket's existing lifecycle configuration, returns early if a rule with the right ID is already present, otherwise merges its rule with any others and writes the configuration back) and tolerant of failure (a single `WARNING` is logged and uploads continue without the rule).

Defaults: prefix `tus-staging/`, rule ID `tus-staging-abort`, 7 days. Both `AbortIncompleteMultipartUpload` (drops in-flight parts) and `Expiration` (defensively deletes any orphaned objects under the prefix) are set.

For auto-apply, the IAM principal needs (in addition to the base zodb-s3blobs permissions):

- `s3:GetLifecycleConfiguration`
- `s3:PutLifecycleConfiguration`

If you don't want to grant these, apply the rule manually once per bucket and the auto-apply call will be a no-op (it sees the existing rule and skips the write).

### Manual setup

`tus-lifecycle.json`:

```json
{
  "Rules": [{
    "ID": "tus-staging-abort",
    "Status": "Enabled",
    "Filter": {"Prefix": "tus-staging/"},
    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
    "Expiration": {"Days": 7}
  }]
}
```

```sh
# AWS S3
aws s3api put-bucket-lifecycle-configuration \
  --bucket <bucket> \
  --lifecycle-configuration file://tus-lifecycle.json

# Tigris (S3-compatible)
aws --endpoint-url=https://fly.storage.tigris.dev \
    s3api put-bucket-lifecycle-configuration \
    --bucket <bucket> \
    --lifecycle-configuration file://tus-lifecycle.json
```

**Important:** `put-bucket-lifecycle-configuration` *replaces* all rules for the bucket.
If the bucket already has lifecycle rules for other purposes, fetch them first with `aws s3api get-bucket-lifecycle-configuration --bucket <bucket>`, append the rule above to the `Rules` array, then put the merged set.
The auto-apply path performs this merge automatically.


## Direct-to-S3 data-plane offload (optional)

By default in S3 mode, every TUS PATCH chunk passes through a Zope worker — Plone reads the request body, calls `upload_part` against S3, and returns 204.
Under high concurrent-upload load this saturates the worker pool because each upload occupies one worker slot continuously.

The data-plane offload removes this bottleneck by having nginx stream each non-final chunk straight to S3, holding a worker only for a small auth/lookup decision (~50 ms instead of ~hundreds of ms per chunk).
The final chunk still routes through Plone so `CompleteMultipartUpload` and content creation run transactionally.

### What's involved

- A new internal endpoint `@tus-authorize` returns a presigned `UploadPart` URL plus routing headers (`X-Route`, `X-S3-Url`, `X-Tus-New-Offset`) for each PATCH.
- nginx's `auth_request` directive consults this endpoint on every `PATCH` to `@tus-upload/<uid>` and either:
  - streams the request body to S3 via the presigned URL (`X-Route: s3`), or
  - forwards the request to Plone unchanged (`X-Route: plone`) — used for the final chunk, local-disk uploads, presign errors, or when the offload is disabled.

### Enabling the offload

Two pieces have to line up:

1. **Plone**: set `TUS_S3_OFFLOAD_ENABLED=1` in the environment of every Zope process. The default is "disabled", in which case `@tus-authorize` always returns `X-Route: plone` and the offload is a no-op (this is the kill switch — flip the env var off and restart to revert without redeploying nginx).
2. **nginx**: deploy a config that runs an `auth_request` against `/_tus_authorize_internal` for `PATCH` requests to `@tus-upload/<uid>`, then proxy-passes to either S3 or Plone based on the response headers. See `plone/conf/nginx/nginx.conf` in the deployment repo for a working configuration.

The Plone-side endpoint is safe to deploy on its own — without the matching nginx config nothing calls it.

### IAM

The Plone IAM principal needs `s3:PutObject` on the staging key pattern (which it already has via the base zodb-s3blobs policy).
No new permissions are required — the presigned URL is signed using the same credentials that handle every other S3 operation.

### Limits and caveats

- nginx returns the S3 200 response status to the client when the chunk is offloaded; Plone returns 204.
  The TUS client-side library (e.g. `@rpldy/chunked-sender`) treats both as success codes, but if you use a custom client check the status handling.
- The `Upload-Offset` response header is computed by Plone in the auth subrequest and emitted by nginx via `add_header`.
  nginx is configured to hide whatever value the upstream (Plone or S3) returned and substitute the authoritative value, so the client always sees the correct offset.
- nginx needs a working `resolver` directive to dispatch `proxy_pass` to the (variable) S3 host.
  In containerised deployments, point at Docker's embedded DNS (`127.0.0.11`); in cloud deployments use the platform-provided resolver.
- For S3-compatible backends with strict header signing (some non-AWS implementations), the presigned URL flow assumes `UNSIGNED-PAYLOAD` for the body — the default in boto3.
  Smoke-test against your specific backend before rolling out widely.

### Smoke-testing the endpoint

With the offload enabled, the authorize endpoint is callable directly:

```sh
curl -i -H "Authorization: Bearer <jwt>" \
     -H "X-Original-Method: PATCH" \
     -H "X-Original-Upload-Offset: 0" \
     -H "X-Original-Content-Length: 10485760" \
     "https://example.com/path/@tus-authorize/<uid>"
```

A successful response is `200 OK` with `X-Route: s3`, `X-S3-Url: https://...`, `X-Tus-New-Offset: 10485760`, and an empty body.
With the env var unset you'll get `X-Route: plone` regardless of input — the offload is off.
