#
# spec file for package os-autoinst-scripts-deps
#
# Copyright 2021 SUSE LLC
#
# All modifications and additions to the file contributed by third parties
# remain the property of their copyright owners, unless otherwise agreed
# upon. The license for this file, and modifications and additions to the
# file, is the same license as for the pristine package itself (unless the
# license for the pristine package is not an Open Source License, in which
# case the license is the MIT License). An "Open Source License" is a
# license that conforms to the Open Source Definition (Version 1.9)
# published by the Open Source Initiative.

# Please submit bugfixes or comments via http://bugs.opensuse.org/
#


%define         base_name os-autoinst-scripts
Name:           %{base_name}-deps
Version:        1
Release:        0
Summary:        Metapackage that contains the dependencies of openQA-related scripts
License:        MIT
Group:          Development/Tools/Other
BuildArch:      noarch
Url:            https://github.com/os-autoinst/%{base_name}
Source0:        %{base_name}-%{version}.tar.xz
# The following line is generated from dependencies.yaml
%define main_requires bash coreutils curl grep html-xml-utils jq openQA-client openssh-clients osc perl >= 5.010 perl(Data::Dumper) perl(FindBin) perl(Getopt::Long) perl(Mojo::File) perl(Text::Markdown) perl(YAML::PP) python3-pynetbox python3-requests python3-sh retry sed sudo xmlstarlet yq
# The following line is generated from dependencies.yaml
%define test_requires perl(Test::MockModule) perl(Test::Most) perl(Test::Output) perl(Test::Warnings) python3-pytest
# The following line is generated from dependencies.yaml
%define devel_requires python3-yamllint shfmt
Requires:       %main_requires
BuildRequires:  make %main_requires %test_requires
Suggests:       salt
Suggests:       postgresql

%description
- auto-review - Automatically detect known issues in openQA jobs, label openQA jobs with ticket references and optionally retrigger
- openqa-investigate - Automatic investigation jobs with failure analysis in openQA

%package devel
Summary:        Development package for %{base_name}-deps
Group:          Development/Tools/Other
BuildRequires:  %devel_requires
Requires:       %devel_requires

%description devel
Development package pulling in all dependencies needed for developing
in %{base_name}-deps

%prep
%setup -n %{base_name}-%{version}

%build

%install

%check
# Can't run bash tests because it needs to clone bpan via git
make test-python

%files

%files devel

%changelog
