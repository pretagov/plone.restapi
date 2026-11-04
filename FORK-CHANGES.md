# Fork changes

This is a fork of [plone/plone.restapi](https://github.com/plone/plone.restapi).
This file lists what the fork changes compared with upstream, and why.

- **Base branch:** `deploy-9`
- **Upstream base:** tag [`9.15.7`](https://github.com/plone/plone.restapi/tree/9.15.7), the version pinned by Plone's 6.0 maintenance branch
- **Fork version:** `9.15.8.dev0`

`deploy-9` is the `9.15.7` tag plus the fork's commits, so to see every change, run:

```sh
git remote add upstream https://github.com/plone/plone.restapi.git
git fetch upstream tag 9.15.7 --no-tags
git diff 9.15.7 deploy-9
```

Update this file in every PR that changes the fork's behaviour.

## Summary

| Area | Change | Main files | Upstream status |
|---|---|---|---|
| TUS | Finished uploads are moved into the blob store instead of being copied | `deserializer/dxfields.py`, `services/content/tus.py` | Fork only |
| TUS | `DELETE @tus-upload/<uid>` cancels an upload (TUS `termination` extension) | `services/content/tus.py`, `services/content/configure.zcml` | Fork only |
| TUS | With a `zodb_s3blobs.S3BlobStorage` ZODB storage, chunks are streamed into an S3 multipart upload instead of a temporary file | `services/content/tus.py` | Fork only |
| TUS | `GET @tus-authorize/<uid>` lets a reverse proxy send chunks straight to S3 (optional, off by default) | `services/content/tus.py`, `services/content/configure.zcml` | Fork only |
| Blocks | Content and site responses include `resolved_uids`, a summary of every object linked from blocks | `serializer/blocks.py`, `serializer/dxcontent.py`, `serializer/site.py`, `serializer/utils.py` | Fork only |
| Control panels | Fields are serialized and deserialized with the DX field and block serializers | `serializer/controlpanels/__init__.py`, `deserializer/controlpanels/__init__.py` | Open upstream as [plone/plone.restapi#1875](https://github.com/plone/plone.restapi/pull/1875) |
| Packaging | Version `9.15.8.dev0` and a changelog entry in `news/` | `setup.py`, `news/+pretagov.feature` | Fork only |

Every change has tests in `tests/test_tus.py` or `tests/test_blocks_serializer.py`. The TUS endpoint docs (`docs/source/endpoints/tusupload.md`) cover the S3 mode, the abandoned-upload lifecycle rule and the proxy offload.

## TUS uploads

### Move instead of copy

Upstream's `NamedFieldDeserializer` reads the finished upload into memory (`value.open()`) and writes it into a new blob.
The fork builds the `NamedBlobFile` with a one-byte placeholder, then calls `TUSUpload.process_blob(blob)`, which moves the upload file into the blob's uncommitted path with `ZODB.blob.rename_or_copy_blob`.
When the TUS temporary directory and the blob directory are on the same volume, this is a rename, so large uploads don't need to fit in memory or be written twice.
The field's cached `size` is set to the upload's real length, because in S3 mode the on-disk blob is only a marker (see below).

### Termination (`DELETE`)

`DELETE @tus-upload/<uid>` removes an in-progress upload and its temporary data (or aborts its S3 multipart upload).
`OPTIONS` now advertises `Tus-Extension: creation,expiration,termination`.
It is registered for `IContentish` (`zope2.View`) and for the site root (`cmf.AddPortalContent`), like upstream's `HEAD` registrations, and checks the same add or modify permission as `PATCH` for the upload's mode.

### S3-backed storage

When the ZODB storage is a [zodb-s3blobs](https://github.com/pretagov/zodb-s3blobs) `S3BlobStorage`, `@tus-upload` uses `S3TUSUpload` in place of the local-disk `TUSUpload`:

- `POST` starts an S3 multipart upload under the `tus-staging/` key prefix.
- Each `PATCH` uploads one part.
- The final `PATCH` completes the multipart upload, and `process_blob` hands the staging key to the storage, which copies it to the blob's key on the server side at commit.
- Upload state (staging key, multipart upload ID, length, chunk size, metadata) is kept in a ZODB annotation on the container (`plone.restapi.tus_uploads`) instead of on local disk, so any Zope process can take any chunk without sticky sessions or a shared temporary directory.
- On the first `POST` in each process, a bucket lifecycle rule (`tus-staging-abort`, 7 days) is installed if it isn't there already, so S3 cleans up abandoned multipart uploads.

zodb-s3blobs is an optional dependency: the import is guarded, and without it (or with a different storage) TUS behaves as upstream does, apart from the move and termination changes above.
The S3 mode needs a zodb-s3blobs version that supports staged blobs.

### Direct-to-S3 chunk offload

`GET @tus-authorize/<uid>` is meant to be called by a reverse proxy, for example through nginx's `auth_request`, before each `PATCH`.
It replies with routing headers: either `X-Route: s3` with a presigned `UploadPart` URL (`X-S3-Url`) and the new offset (`X-Tus-New-Offset`), or `X-Route: plone`.
With `X-Route: s3` the proxy streams the chunk body straight to S3, so a Zope worker isn't held for the whole transfer.
The final chunk, local-disk uploads and presign errors always go to Plone.

The offload is off unless `TUS_S3_OFFLOAD_ENABLED=1` is set for every Zope process. While it's off, `@tus-authorize` always returns `X-Route: plone`, and without a matching proxy configuration nothing calls the endpoint.

## `resolved_uids`

Content (`SerializeToJson`) and site root (`SerializeSiteRootToJson`) responses gain a `resolved_uids` key.
It maps each URL that a block's `resolveuid` link resolved to the `ISerializeToJsonSummary` of the target object.
A frontend can then render block links (titles, types, review states) without an extra request per link.

- The mapping is collected on a request annotation (`plone.restapi.serializer.blocks.resolved_objects`) while the blocks are serialized.
- `ResolveUIDSerializerBase` records each object it resolves. If blocks are serialized outside the content or site serializers (control panels, or a field serializer on its own), the annotation is created on demand.
- `serializer.utils.resolve_uid` returns the brain for primary-field targets too (upstream returns `None`), so those links are recorded as well.

## Control panels

Upstream serializes control panel fields by temporarily marking the registry proxy with `IDexterityContent`, and deserializes against a bare fake context. Block fields in control panels are therefore not processed by the block serializers, so `resolveuid` links aren't resolved.

The fork, following [plone/plone.restapi#1875](https://github.com/plone/plone.restapi/pull/1875):

- serializes against a fake context that provides `IDexterityContent`, `IBlocks` and the control panel's schema, with the registry values copied onto it
- deserializes against a fake context that provides `IDexterityContent` and `IBlocks`
- wraps both fake contexts in an acquisition wrapper of the real context, so serializers that need the site (for example to resolve UIDs) can find it

The registry proxy is no longer modified.

## Not carried over from the older fork

The fork's earlier `deploy` branch (8.x) also had the following. They aren't needed on `deploy-9` because upstream already has them by 9.15.7:

- the security fixes for GHSA-8rqh-vxpr-x77p, GHSA-r3g9-vgf8-vv35 and GHSA-qp9m-v45c-4g3p
- the form memory limit patch (upstream 9.1.2)
- `resolveuid` support in nested schemas (upstream 8.38)
- the TUS upload location fix (upstream 9.0.0)
