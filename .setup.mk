SHELL := bash

.DELETE_ON_ERROR:
.SECONDEXPANSION:

GIT_STATUS_IS_CLEAN := $(shell if [ ! -d .git ] || [ -z "$$(git diff --stat 2>/dev/null)" ]; then echo 1; fi)
