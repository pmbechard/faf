# Copyright (C) 2013  ABRT Team
# Copyright (C) 2013  Red Hat, Inc.
#
# This file is part of faf.
#
# faf is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# faf is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with faf.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import absolute_import

from typing import Dict, List, Union

from datetime import datetime
import fnmatch
import json
from urllib.error import HTTPError
from urllib.request import urlopen

import koji

from pyfaf.opsys import System
from pyfaf.checker import DictChecker, IntChecker, ListChecker, StringChecker
from pyfaf.common import FafError, log
from pyfaf.queries import (get_arch_by_name,
                           get_opsys_by_name,
                           get_osrelease,
                           get_package_by_nevra,
                           get_reportpackage,
                           get_report_release_desktop,
                           get_unknown_package)
from pyfaf.storage import (Arch,
                           Build,
                           OpSys,
                           Package,
                           ReportReleaseDesktop,
                           ReportPackage,
                           ReportUnknownPackage,
                           column_len)
from pyfaf.utils.parse import str2bool, words2list
from pyfaf.storage.custom_types import to_semver


__all__ = ["Fedora"]


class Fedora(System):
    name = "fedora"
    nice_name = "Fedora"

    packages_checker = ListChecker(
        DictChecker({
            "name":            StringChecker(pattern=r"^[a-zA-Z0-9_\-\.\+~]+$",
                                             maxlen=column_len(Package,
                                                               "name")),
            "epoch":           IntChecker(minval=0),
            "version":         StringChecker(pattern=r"^[a-zA-Z0-9_\.\+~]+$",
                                             maxlen=column_len(Build, "version")),
            "release":         StringChecker(pattern=r"^[a-zA-Z0-9_\.\+]+$",
                                             maxlen=column_len(Build, "release")),
            "architecture":    StringChecker(pattern=r"^[a-zA-Z0-9_]+$",
                                             maxlen=column_len(Arch, "name")),
        }), minlen=0
    )

    ureport_checker = DictChecker({
        # no need to check name, version and architecture twice
        # the toplevel checker already did it
        # "name": StringChecker(allowed=[Fedora.name])
        # "version":        StringChecker()
        # "architecture":   StringChecker()

        "desktop": StringChecker(mandatory=False, pattern=r"^[a-zA-Z0-9_/-]+$",
                                 maxlen=column_len(ReportReleaseDesktop,
                                                   "desktop"))
    })

    pkg_roles = ["affected", "related", "selinux_policy"]

    @classmethod
    def install(cls, db, logger=None) -> None:
        if logger is None:
            logger = log.getChild(cls.__name__)

        logger.info("Adding Fedora operating system")
        new = OpSys()
        new.name = cls.nice_name
        db.session.add(new)
        db.session.flush()

    @classmethod
    def installed(cls, db) -> bool:
        return bool(get_opsys_by_name(db, cls.nice_name))

    def __init__(self) -> None:
        super().__init__()
        self.eol = None
        self.pdc_url = None
        self.pagure_url = None
        self.build_aging_days = None
        self.koji_url = None
        self.ignored_releases = []
        self.allow_unpackaged = None
        self.load_config_to_self("eol", ["fedora.supporteol"],
                                 False, callback=str2bool)
        self.load_config_to_self("pdc_url", ["fedora.fedorapdc"],
                                 "https://pdc.fedoraproject.org/rest_api/v1/")
        self.load_config_to_self("pagure_url", ["fedora.pagureapi"],
                                 "https://src.fedoraproject.org/api/0/")
        self.load_config_to_self("build_aging_days",
                                 ["fedora.build-aging-days"],
                                 7, callback=int)
        self.load_config_to_self("koji_url",
                                 ["fedora.koji-url"], None)
        self.load_config_to_self("ignored_releases",
                                 ["fedora.ignored-releases"], [],
                                 callback=words2list)
        self.load_config_to_self("allow_unpackaged",
                                 ["ureport.allow-unpackaged"], False,
                                 callback=str2bool)

    def _save_packages(self, db, db_report, packages, count=1) -> None:
        for package in packages:
            role = "RELATED"
            if "package_role" in package:
                if package["package_role"] == "affected":
                    role = "CRASHED"
                elif package["package_role"] == "selinux_policy":
                    role = "SELINUX_POLICY"

            db_package = get_package_by_nevra(db,
                                              name=package["name"],
                                              epoch=package["epoch"],
                                              version=package["version"],
                                              release=package["release"],
                                              arch=package["architecture"])
            if db_package is None:
                self.log_warn("Package {0}-{1}:{2}-{3}.{4} not found in "
                              "storage".format(package["name"],
                                               package["epoch"],
                                               package["version"],
                                               package["release"],
                                               package["architecture"]))

                db_unknown_pkg = get_unknown_package(db,
                                                     db_report,
                                                     role,
                                                     package["name"],
                                                     package["epoch"],
                                                     package["version"],
                                                     package["release"],
                                                     package["architecture"])
                if db_unknown_pkg is None:
                    db_arch = get_arch_by_name(db, package["architecture"])
                    if db_arch is None:
                        continue

                    db_unknown_pkg = ReportUnknownPackage()
                    db_unknown_pkg.report = db_report
                    db_unknown_pkg.name = package["name"]
                    db_unknown_pkg.epoch = package["epoch"]
                    db_unknown_pkg.version = package["version"]
                    db_unknown_pkg.release = package["release"]
                    db_unknown_pkg.semver = to_semver(package["version"])
                    db_unknown_pkg.semrel = to_semver(package["release"])
                    db_unknown_pkg.arch = db_arch
                    db_unknown_pkg.type = role
                    db_unknown_pkg.count = 0
                    db.session.add(db_unknown_pkg)

                db_unknown_pkg.count += count
                continue

            db_reportpackage = get_reportpackage(db, db_report, db_package)
            if db_reportpackage is None:
                db_reportpackage = ReportPackage()
                db_reportpackage.report = db_report
                db_reportpackage.installed_package = db_package
                db_reportpackage.count = 0
                db_reportpackage.type = role
                db.session.add(db_reportpackage)

            db_reportpackage.count += count

    def validate_ureport(self, ureport) -> bool:
        Fedora.ureport_checker.check(ureport)
        return True

    def validate_packages(self, packages) -> bool:
        affected = False
        Fedora.packages_checker.check(packages)
        for package in packages:
            if "package_role" in package:
                if package["package_role"] not in Fedora.pkg_roles:
                    raise FafError("Only the following package roles are allowed: "
                                   "{0}".format(", ".join(Fedora.pkg_roles)))
                if package["package_role"] == "affected":
                    affected = True

        if not (affected or self.allow_unpackaged):
            raise FafError("uReport must contain affected package")

        return True

    def save_ureport(self, db, db_report, ureport, packages, flush=False, count=1) -> None:
        if "desktop" in ureport:
            db_release = get_osrelease(db, Fedora.nice_name, ureport["version"])
            if db_release is None:
                self.log_warn("Release '{0} {1}' not found"
                              .format(Fedora.nice_name, ureport["version"]))
            else:
                db_reldesktop = get_report_release_desktop(db, db_report,
                                                           db_release,
                                                           ureport["desktop"])
                if db_reldesktop is None:
                    db_reldesktop = ReportReleaseDesktop()
                    db_reldesktop.report = db_report
                    db_reldesktop.release = db_release
                    db_reldesktop.desktop = ureport["desktop"]
                    db_reldesktop.count = 0
                    db.session.add(db_reldesktop)

                db_reldesktop.count += count

        self._save_packages(db, db_report, packages, count=count)

        if flush:
            db.session.flush()

    def get_releases(self) -> Dict[str, Dict[str, str]]:
        result = {}
        # Page size -1 means, that all results are on one page
        url = f"{self.pdc_url}releases/?page_size=-1&short={Fedora.name}"

        with urlopen(url) as response:
            releases = json.load(response)

        for release in releases:
            ver = release["version"].lower()

            # only accept Fedora version with decimals (or rawhide)
            if not ver.isdecimal() and ver != "rawhide":
                continue

            # Only accept GA releases, i.e. skip updates and updates-testing
            # pseudo-releases. Moreover check for specifically ignored releases.
            if release.get("release_type") != "ga" or self._is_ignored(ver):
                continue

            result[ver] = {"status": "ACTIVE" if release["active"] else "EOL"}

        return result

    def get_components(self, release) -> List[str]:
        branch = self._release_to_branch(release)

        result = []
        url = (f"{self.pdc_url}component-branches/?name={branch}&page_size=-1"
               "&fields=global_component&type=rpm")

        with urlopen(url) as response:
            components = json.load(response)

        for item in components:
            result.append(item["global_component"])

        return result

    def get_component_acls(self, component) -> Dict[str, Dict[str, bool]]:
        result: Dict[str, Dict[str, bool]] = {}
        url = f"{self.pagure_url}/rpms/{component}"

        try:
            with urlopen(url) as response:
                acls = json.load(response)
        except HTTPError as ex:
            self.log_error("Unable to get package information for component '%s': %s\n\tURL: %s",
                           component, str(ex), url)
            return result

        for user_g in acls["access_users"]:
            for user in acls["access_users"][user_g]:
                result[user] = {"commit": True, "watchbugzilla": False}

        # Check for watchers
        url += "/watchers"
        try:
            with urlopen(url) as response:
                watchers = json.load(response)
        except HTTPError as ex:
            self.log_error("Unable to get watchers for component '%s': %s\n\tURL: %s",
                           component, str(ex), url)
            return result

        for user in watchers["watchers"]:
            if user in result.keys():
                result[user]["watchbugzilla"] = True
            else:
                result[user] = {"commit": False, "watchbugzilla": True}

        return result

    def get_build_candidates(self, db) -> List[Build]:
        return (db.session.query(Build)
                .filter(Build.release.like("%%.fc%%"))
                .all())

    def check_pkgname_match(self, packages, parser) -> bool:
        for package in packages:
            if (not "package_role" in package or
                    package["package_role"].lower() != "affected"):
                continue

            nvra = "{0}-{1}-{2}.{3}".format(package["name"],
                                            package["version"],
                                            package["release"],
                                            package["architecture"])

            match = parser.match(nvra)
            if match is not None:
                return True

        return False

    def _release_to_branch(self, release) -> str:
        """
        Convert faf's release to branch name
        """

        if not isinstance(release, str):
            release = str(release)

        if release.lower() == "rawhide":
            branch = "rawhide"
        elif release.isdigit():
            int_release = int(release)
            if int_release < 6:
                branch = "FC-{0}".format(int_release)
            elif int_release == 6:
                branch = "fc{0}".format(int_release)
            else:
                branch = "f{0}".format(int_release)
        else:
            raise FafError("{0} is not a valid Fedora version".format(release))

        return branch

    def get_released_builds(self, release) -> List[Dict[str, Union[str, int, datetime]]]:
        session = koji.ClientSession(self.koji_url)
        builds_release = session.listTagged(tag="f{0}".format(release),
                                            inherit=False)
        builds_updates = session.listTagged(tag="f{0}-updates".format(release),
                                            inherit=False)

        return [{"name": b["name"],
                 "epoch": b["epoch"] if b["epoch"] is not None else 0,
                 "version": b["version"],
                 "release": b["release"],
                 "nvr": b["nvr"],
                 "completion_time": datetime.strptime(b["completion_time"],
                                                      "%Y-%m-%d %H:%M:%S.%f")
                } for b in sorted(builds_release+builds_updates,
                                  key=lambda b: b["completion_time"],
                                  reverse=True)]

    def _is_ignored(self, ver) -> bool:
        """
        Check if the release version matches any of the glob-like patterns specified
        in the configuration option 'ignored-releases'.
        """

        for pattern in self.ignored_releases:
            if fnmatch.fnmatchcase(ver, pattern):
                return True

        return False
