Name: tmt-plugin-diana
Version: 0.0.1
Release: 1%{?dist}

Summary: TMT Diana provisioner plugin
License: BSD
BuildArch: noarch

URL: https://github.com/Conscript89/tmt-plugin-diana
Source0: https://github.com/Conscript89/tmt-plugin-diana/releases/download/%{version}/tmt-plugin-diana-%{version}.tar.gz

%generate_buildrequires
%pyproject_buildrequires

%description
TMT Diana provisioner plugin that allows to use libvirt hypervisor and fully customise provisioned system (including kickstart and hardware specification).

%prep
%autosetup


%build
%pyproject_wheel


%install
%pyproject_install

%package -n tmt-provision-diana
Requires: tmt
Summary: TMT Diana provisioner plugin

%description -n tmt-provision-diana
TMT Diana provisioner plugin that allows to use libvirt hypervisor and fully customise provisioned system (including kickstart and hardware specification).

%files -n tmt-provision-diana
%pycached %{python3_sitelib}/tmt/steps/provision/diana.py
%{python3_sitelib}/tmt.steps.provision.diana-*.dist-info/

%changelog
* Sun Jul 09 2023 Pavel Holica <conscript89@gmail.com> - 0.0.1-1
- Initial specfile
