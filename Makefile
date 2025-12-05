SH_FILES ?= $(shell file --mime-type $$(git ls-files) test/*.t | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p')

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

test: checkstyle test-unit

test-unit: test-bash test-python

test-bash: $(BPAN)
	prove -r $(if $v,-v )$(test)

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

test-shellcheck:
	@which shfmt >/dev/null 2>&1 || echo "Command 'shfmt' not found, can not execute shell script formating checks"
	shfmt -d ${SH_FILES}
	@which shellcheck >/dev/null 2>&1 || echo "Command 'shellcheck' not found, can not execute shell script checks"
	files=$$(file --mime-type * | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p'); if [ -n "$$files" ]; then shellcheck -x $$(file --mime-type * | sed -n 's/^\(.*\):.*text\/x-shellscript.*$$/\1/p'); fi

test-yaml:
	@which yamllint >/dev/null 2>&1 || echo "Command 'yamllint' not found, can not execute YAML syntax checks"
	yamllint --strict $$(git ls-files "*.yml" "*.yaml" ":!external/")

checkstyle-python:
	@which ruff >/dev/null 2>&1 || echo "Command 'ruff' not found, can not execute python style checks"
	ruff format --check && ruff check

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
