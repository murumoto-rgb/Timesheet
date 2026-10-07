-- Apply once during deployment. Runtime code never creates or alters schema.
CREATE SCHEMA IF NOT EXISTS timesheet_private;

REVOKE ALL ON SCHEMA timesheet_private FROM PUBLIC;

CREATE TABLE IF NOT EXISTS timesheet_private.encrypted_blobs (
    id INTEGER PRIMARY KEY,
    data BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT encrypted_blobs_nonempty CHECK (octet_length(data) > 0)
);

REVOKE ALL ON TABLE timesheet_private.encrypted_blobs FROM PUBLIC;
