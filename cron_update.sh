#!/bin/bash

set -e -o pipefail

cd /cds/home/k/klauer/Repos/happi-to-confluence

update() {
  echo "* Updating at $(date)"
  source /reg/g/pcds/engineering_tools/latest-released/scripts/pcds_conda "" || echo "Sourcing failed?"
  echo "* Environment sourced; working directory:"
  pwd
  echo "* Making pages..."
  make -s prod-pages 2>&1
  echo "* Done!"
}

update | tee -a cron_update_log.txt
