all: happi.json

happi.json:
	python -m whatrecord.plugins.happi > happi_info.json

pages:
	python generate.py

.PHONY: pages
