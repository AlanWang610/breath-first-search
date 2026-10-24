# Shape fixtures

A payload's *shape*, written by hand from the publisher's documentation with invented values -
for sources whose payloads carry no redistribution grant and so cannot be committed as
cassettes (ADR 0038, ADR 0055).

`<family>_<name>.json`, each with a `_provenance` block naming the documentation it was
written from, the date it was read, and `"values": "invented"`, beside the `payload` a parser
reads. `tests/contract/test_shape_fixtures.py` holds every file here to that, and a `network`
test per family checks the live payload still carries the fixture's keys.

Never derive one from a recorded payload: that is a cassette with its licence filed off.
