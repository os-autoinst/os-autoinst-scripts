SH_FILES ?= $(shell file --mime-type $$(git ls-files) test/*.t | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')
SH_SHELLCHECK_FILES ?= $(shell file --mime-type * | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')
PY_FILES ?= $(shell git ls-files | xargs file --mime-type 2>/dev/null | grep -E 'text/x-script\.python|text/x-python' | cut -d: -f1)

ifndef CI
include .setup.mk
endif

ifndef test
test := test/
ifdef GIT_STATUS_IS_CLEAN
test += xt/
endif
endif

PROVE ?= tools/prove_wrapper
BPAN := .bpan

#------------------------------------------------------------------------------
# User targets
#------------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@grep -h -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

default: help

.PHONY: test
ifeq ($(CHECKSTYLE),0)
checkstyle_tests =
else
checkstyle_tests = checkstyle
endif
test: $(checkstyle_tests) test-unit ## Run style checks and unit tests

test-unit: test-bash test-python ## Run all unit tests (bash and python)

test-bash: $(BPAN) ## Run bash tests
	"${PROVE}" -r $(if $v,-v )$(test)

test-python: ## Run python tests
	py.test tests

test-online: ## Run tests that require online connection
	dry_run=1 bash -x ./openqa-label-known-issues-multi < ./tests/incompletes
	dry_run=1 ./trigger-openqa_in_openqa
	# Invalid JSON causes the job to abort with an error
	-tw_openqa_host=example.com dry_run=1 ./trigger-openqa_in_openqa

checkstyle: test-shellcheck test-yaml checkstyle-python check-code-health test-gitlint ## Run all style checks

shfmt: ## Format shell scripts
	shfmt -w ${SH_FILES}

test-shellcheck: ## Run shell script checks
	@which shfmt >/dev/null 2>&1 || echo "Command 'shfmt' not found, can not execute shell script formating checks"
	shfmt -d ${SH_FILES}
	@which shellcheck >/dev/null 2>&1 || echo "Command 'shellcheck' not found, can not execute shell script checks"
	if [ -n "${SH_SHELLCHECK_FILES}" ]; then shellcheck -x ${SH_SHELLCHECK_FILES}; fi

test-yaml: ## Run YAML syntax checks
	@which yamllint >/dev/null 2>&1 || echo "Command 'yamllint' not found, can not execute YAML syntax checks"
	yamllint --strict $$(git ls-files "*.yml" "*.yaml" ":!external/")

checkstyle-python: check-ruff check-conventions check-ty ## Run python style checks
check-ruff: ## Run python style checks with ruff
	@which ruff >/dev/null 2>&1 || echo "Command 'ruff' not found, can not execute python style checks"
	@if [ -n "$(PY_FILES)" ]; then ruff format --check $(PY_FILES) && ruff check $(PY_FILES); fi

check-conventions: ## Check project conventions
	@if git grep -nE '^\s*@(unittest\.mock\.|mock\.)?patch' tests/; then \
		echo "Error: @patch decorator detected. Avoid to prevent argument ordering bugs."; \
		echo "   Fix: Use the 'mocker' fixture (pytest-mock) or a 'with patch():' context manager."; \
		exit 1; \
	fi

.PHONY: check-ty
check-ty: ## Run ty type checker
	ty check

check-code-health: ## Run code health checks (vulture)
	@echo "Checking code health…"
	@vulture $$(git ls-files "**.py") --min-confidence 80

.PHONY: test-gitlint
test-gitlint: ## Run commit message checks using gitlint
	@command -v gitlint >/dev/null 2>&1 || (echo "Command 'gitlint' not found, can not execute commit message checks. Install with 'python3-gitlint' (openSUSE) or 'pip install gitlint-core'" && false)
	@BASES=$$(for i in upstream/master upstream/main origin/master origin/main master main; do git rev-parse --verify $$i 2>/dev/null; done ||:); \
	BASE=$$(git merge-base --independent $$BASES | head -n 1); \
	gitlint --commits "$$BASE..HEAD"

.PHONY: tidy
tidy: ## Format code and fix linting issues
	ruff format $(PY_FILES)
	ruff check --fix $(PY_FILES)

update-deps: ## Update dependencies package spec and cpanfile
	tools/update-deps --cpanfile cpanfile --specfile dist/rpm/os-autoinst-scripts-deps.spec

clean: ## Clean up generated files
	$(RM) job_post_response
	$(RM) -r $(BPAN)
	$(RM) -r .pytest_cache/
	find . -name __pycache__ | xargs -r $(RM) -r

install-systemd-local: ## Install contained systemd units for local use (not meant for packaging)
	install -d -m 755 "$(DESTDIR)"/etc/systemd/system
	for i in systemd/*.{service,timer}; do \
		install -m 644 $$i "$(DESTDIR)"/etc/systemd/system ;\
	done
	install -d -m 755 "$(DESTDIR)"/etc/systemd/user
	for i in systemd/user/*.{service,timer}; do \
		install -m 644 $$i "$(DESTDIR)"/etc/systemd/user ;\
	done
	find "$(DESTDIR)"/etc/systemd -name '*.service' -exec sed -i -e "s|/opt/os-autoinst-scripts|$(PWD)|g" {} \+

#------------------------------------------------------------------------------
# Internal targets
#------------------------------------------------------------------------------
$(BPAN):
	git clone https://github.com/bpan-org/bpan.git --depth 1 $@
