#!/usr/bin/env bash
# Pull local project code from the kryx-dev environment on the Kryx VPS
rsync -auv --exclude-from="/Users/phillip/Downloads/kryx/.rsync-exclude" kryx-cloud:~/kryx-dev/ /Users/phillip/Downloads/kryx/
