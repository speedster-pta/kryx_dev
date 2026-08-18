#!/usr/bin/env bash
# Promote tested code from kryx-dev to production (kryx), local rsync on this server.
# docker-compose.yml, .env, and data/ are excluded so each environment keeps its own config.
rsync -auv --exclude-from="$HOME/kryx-dev/.rsync-exclude" "$HOME/kryx-dev/" "$HOME/kryx/"
