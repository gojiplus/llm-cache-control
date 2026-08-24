# Draft rendering and checks.
#
# The draft is kramdown-rfc markdown. The .xml and .txt are build output and
# are gitignored: regenerate them, do not edit them.

GEMBIN := $(HOME)/.local/share/gem/ruby/4.0.0/bin
DRAFT  := draft-sood-llm-cache-control-01

.PHONY: draft check test lint clean tools

draft: $(DRAFT).txt

$(DRAFT).xml: $(DRAFT).md
	PATH="$(GEMBIN):$$PATH" kramdown-rfc $< > $@

$(DRAFT).txt: $(DRAFT).xml
	xml2rfc --text $< -o $@

tools:
	gem install --user-install kramdown-rfc
	python3 -m pip install xml2rfc

check: test lint

test:
	python3 -m conformance.run_conformance
	python3 -m conformance.minimality
	python3 -m pytest gateway/tests -q

lint:
	python3 -m black --line-length 100 --check conformance/ gateway/
	python3 -m isort --settings-path conformance/setup.cfg --check conformance/ gateway/
	python3 -m flake8 --config conformance/setup.cfg conformance/ gateway/

clean:
	rm -f $(DRAFT).xml $(DRAFT).txt
	rm -rf .refcache
