all: pages

happi_info.json: /cds/group/pcds/pyps/apps/hutch-python/device_config/db.json
	python -m whatrecord.plugins.happi > $@

pages: happi_info.json
	/bin/bash -c " \
		source confluence.sh && \
			python generate.py \
	"

.PHONY: pages
