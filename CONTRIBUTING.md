# Contributing

The most valuable contribution to this repository is a second implementation,
and the way to make it valuable is counterintuitive, so it is first.

## If you are writing a second implementation

**Read the specification. Do not read `gateway/` or `conformance/`.**

Read `draft-sood-llm-cache-control-01.md`, `binding-openai-chat.md`, and the
three schemas. Nothing else. Once you have read the reference implementation
you can no longer find the thing that matters, because you will resolve every
ambiguous sentence the way it resolved them, and the ambiguity becomes
invisible to you.

This is not a hypothetical. A reader run exactly this way found sixteen places
the specification did not determine an implementation, five of which became
normative changes. The sharpest: two implementations independently chose
length-prefixed SHA-256 for the namespace digest, differed only in how they
encoded the length, both conformed, and could not reuse a single cache entry
the other wrote. No amount of testing against the reference implementation
would have surfaced that, because the reference implementation was one of the
two. The golden vectors in `llm-cache-control-test-vectors.json` exist because
of it.

## What to report

Divergence from the reference implementation is a **finding about the
specification**, not a bug report. Both implementations may be conforming; that
is the problem.

For each one, give:

- The specification text that is underdetermined, quoted, with its section. Or
  say plainly that the specification is silent.
- The reading you chose, and why.
- At least one other defensible reading.
- Whether the two readings differ observably, with a concrete input where they
  do.

Open an issue with that. If the answer is that the document must change, it
usually must.

## Running the checks

```
pip install jsonschema pytest
make check
```

`make check` runs three suites. `conformance.run_conformance` validates every
JSON example, composes the test vectors and compares against expected effective
policy, runs the negative cases, exercises the behavioral requirements against
the gateway, and prints a coverage report. `conformance.minimality` tests
whether each constraint and hint earns its place. `pytest gateway/tests` covers
the internals.

Live tests against a real inference server skip unless `VLLM_BASE_URL` is set,
and the skip says the binding is unverified rather than passing quietly.

## Rendering the draft

```
make tools    # once: kramdown-rfc and xml2rfc
make draft
```

The `.xml` and `.txt` are build output and are gitignored. Edit the `.md`.

## Changing the specification

A change to the draft or the binding should come with the thing that would have
caught the problem: a test vector, a conformance case, or a golden value. The
namespace digest has golden vectors for exactly this reason, and they exist
because a construction that was merely described rather than specified produced
two incompatible implementations.

If you change composition, the mutation check is the standard: break the rule
on purpose and confirm the suite fails on the specific case that covers it. A
check that cannot fail is not evidence.

## Licensing

Code is BSD-3-Clause, see `LICENSE`. The Internet-Draft carries the IETF Trust
provisions declared in its front matter (`ipr: trust200902`). Contributions are
accepted under those same terms.
