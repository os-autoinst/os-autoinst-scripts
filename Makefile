SH_FILES ?= $(shell file --mime-type $$(git ls-files) test/*.t | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')
SH_SHELLCHECK_FILES ?= $(shell file --mime-type * | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')

ifndef CI
include .setup.mk
endif

ifndef test
test := test/
ifdef GIT_STATUS_IS_CLEAN
test += xt/
endif
endif

BPAN := .bpan


#------------------------------------------------------------------------------
# User targets
#------------------------------------------------------------------------------
default:

.PHONY: test
ifeq ($(CHECKSTYLE),0)
checkstyle_tests =
else
checkstyle_tests = checkstyle
endif
test: $(checkstyle_tests) test-unit

test-unit: test-bash test-python

test-bash: $(BPAN)
	prove -r $(if $v,-v )$(test)

.PHONY: test-python
test-python:
	py.test tests

test-online:
	dry_run=1 bash -x ./openqa-label-known-issues-multi < ./tests/incompletes
	dry_run=1 ./trigger-openqa_in_openqa
	# Invalid JSON causes the job to abort with an error
	-tw_openqa_host=example.com dry_run=1 ./trigger-openqa_in_openqa

checkstyle: test-shellcheck test-yaml checkstyle-python

shfmt:
	shfmt -w ${SH_FILES}

.PHONY: tidy
tidy:
	ruff format
	ruff check --fix

test-shellcheck:
	@which shfmt >/dev/null 2>&1 || echo "Command 'shfmt' not found, can not execute shell script formating checks"
	shfmt -d ${SH_FILES}
	@which shellcheck >/dev/null 2>&1 || echo "Command 'shellcheck' not found, can not execute shell script checks"
	if [ -n "${SH_SHELLCHECK_FILES}" ]; then shellcheck -x ${SH_SHELLCHECK_FILES}; fi

test-yaml:
	@which yamllint >/dev/null 2>&1 || echo "Command 'yamllint' not found, can not execute YAML syntax checks"
	yamllint --strict $$(git ls-files "*.yml" "*.yaml" ":!external/")

.PHONY: checkstyle-python
checkstyle-python:
	@which ruff >/dev/null 2>&1 || echo "Command 'ruff' not found, can not execute python style checks"
	ruff format --check && ruff check

.PHONY: test-with-coverage
test-with-coverage:
	pytest --cov=src/os-autoinst-scripts tests/

update-deps:
	tools/update-deps --cpanfile cpanfile --specfile dist/rpm/os-autoinst-scripts-deps.spec

clean:
	$(RM) job_post_response
	$(RM) -r $(BPAN)
	$(RM) -r .pytest_cache/
	find . -name __pycache__ | xargs -r $(RM) -r

#------------------------------------------------------------------------------
# Internal targets
#------------------------------------------------------------------------------
$(BPAN):
	git clone https://github.com/bpan-org/bpan.git --depth 1 $@
