all: pages

happi_info.json:
	python -m whatrecord.plugins.happi > $@

pages:
	/bin/bash -c " \
		source confluence.sh && \
			python -m whatrecord.plugins.happi > happi_info.json && \
			python generate.py \
	"

.PHONY: pages
