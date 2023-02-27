GENERATE_ARGS ?=

all: prod-pages

clean:
	rm -f happi_info.json .happi_info.json

check:
	if [[ -f happi_info.json && $$(stat --format="%s" happi_info.json ) -eq 0 ]]; then \
		echo "Removing stale/invalid happi_info.json"; \
		rm -f happi_info.json; \
	fi \

happi_info.json: /cds/group/pcds/pyps/apps/hutch-python/device_config/db.json
	/bin/bash -c " \
		rm -f ".${@}" && \
			source /reg/g/pcds/engineering_tools/latest-released/scripts/pcds_conda && \
			python -m whatrecord.plugins.happi > ".${@}" \
	"
	mv ".${@}" "$@"

prod-pages: check happi_info.json
	/bin/bash -c " \
		source confluence.sh && \
			ipython --pdb generate.py -- --production $(GENERATE_ARGS) \
	"

dev-pages: check happi_info.json
	/bin/bash -c " \
		source confluence.sh && \
			ipython --pdb generate.py -- $(GENERATE_ARGS) \
	"

.PHONY: all clean check dev-pages
