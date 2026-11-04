pretagov fork: TUS uploads move the finished upload into the blob store instead of copying it, support the termination extension (``DELETE``), and can stream chunks to S3 via zodb-s3blobs, optionally offloading them to a reverse proxy with ``@tus-authorize``.
Content and site responses include ``resolved_uids``, a summary of each object linked from blocks.
Control panel fields are serialized with the DX field and block serializers (upstream PR 1875).
