Copies of kami-lens `src/queries/schemas/*.json`, one directory per
contract shape the keeper's client distinguishes (SPEC D1; the tested
daemons 1.0.5 and 1.1.0 serve the 1.0.0 shape):

- `0.6.1/` — kami-lens 0.6.1 (`f07209d`). The shape a 0.6.3 daemon still
  serves (0.6.2 and 0.6.3 added no query schema the keeper reads).
- `1.0.0/` — kami-lens 1.0.0 (`0ffc8a7`):
  `node`, `party`, `status`, `kami`, `roster` as published there, plus
  `account` and `inventory` copied from `0.6.1/` because 1.0.0 changed
  neither for the keeper's reads (the `item`/`items` rows of the two ERC20
  items gain `token`; the 4242 inventory fixture holds none of them).

Used by `tests/test_fakelens.py`, which validates every fake lens answer
against the schema set of the version the fake is serving — the fake
invents VALUES, never FIELDS. Refresh together with the SPEC D1 pin.
