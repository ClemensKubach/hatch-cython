import os
import subprocess
import sys
from contextlib import contextmanager
from glob import glob, iglob
from tempfile import TemporaryDirectory
from typing import Dict

import pathspec
from Cython.Tempita import sub as render_template
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from pathspec import PathSpec

from hatch_cython.config import parse_from_dict, Config
from hatch_cython.constants import (
    compiled_extensions,
    intermediate_extensions,
    precompiled_extensions,
    templated_extensions,
)
from hatch_cython.temp import ExtensionArg, setup_py
from hatch_cython.types import CallableT, DictT, ListStr, ListT, P, Set
from hatch_cython.utils import autogenerated, memo, parse_user_glob, plat


RelAbsPathMap = Dict[str, str]


def remove_leading_dot(path: str) -> str:
    if path.startswith("./"):
        return path[1:]
    return path


def filter_ensure_wanted(wanted: CallableT[[str], bool] , tgts: ListStr):
    return list(
        filter(
            wanted,
            tgts,
        )
    )


class CythonBuildHook(BuildHookInterface):
    PLUGIN_NAME = "cython"

    precompiled_extensions: Set[str]
    intermediate_extensions: Set[str]
    templated_extensions: Set[str]
    compiled_extensions: Set[str]

    def __init__(self, *args: P.args, **kwargs: P.kwargs):
        self.precompiled_extensions = precompiled_extensions.copy()
        self.intermediate_extensions = intermediate_extensions.copy()
        self.templated_extensions = templated_extensions.copy()
        self.compiled_extensions = compiled_extensions.copy()

        super().__init__(*args, **kwargs)

        _ = self.options

    @property
    @memo
    def is_src(self) -> bool:
        return os.path.exists(os.path.join(self.root, "src"))

    @property
    def is_windows(self) -> bool:
        return plat() == "windows"

    def normalize_path(self, pattern: str) -> str:
        if self.is_windows:
            return pattern.replace("/", "\\")
        return pattern.replace("\\", "/")

    def normalize_glob(self, pattern: str):
        return pattern.replace("\\", "/")

    @property
    @memo
    def dir_name(self):
        """Namespace package or package name"""
        return self.options.src if self.options.src is not None else self.metadata.name

    @property
    @memo
    def project_dir(self):
        if self.is_src:
            src = f"src/{self.dir_name}"
        else:
            src = f"{self.dir_name}"
        return src

    def render_templates(self):
        for template in list(self.templated_files.keys()):
            outfile = template[:-3]
            with open(template, encoding="utf-8") as f:
                tmpl = f.read()

            kwds = self.options.templates.find(self, outfile, template)
            data = render_template(tmpl, **kwds)
            with open(outfile, "w", encoding="utf-8") as f:
                f.write(autogenerated(kwds) + "\n\n" + data)

    @property
    @memo
    def precompiled_globs(self):
        _globs = []
        for ex in self.precompiled_extensions:
            _globs.extend((f"{self.project_dir}/*{ex}", f"{self.project_dir}/**/*{ex}"))
        return list(set(_globs))

    @property
    @memo
    def options_exclude(self):
        return [e.matches for e in self.options.files.exclude if e.applies()]

    @property
    @memo
    def options_include(self):
        return [e.matches for e in self.options.files.targets if e.applies()]

    @property
    @memo
    def options_exclude_compiled_src(self):
        return [e.matches for e in self.options.files.exclude_compiled_src if e.applies()]

    @property
    @memo
    def options_include_compiled_src(self):
        return [e.matches for e in self.options.files.include_compiled_src if e.applies()]

    @property
    @memo
    def exclude_spec(self) -> PathSpec:
        return PathSpec.from_lines(
            pathspec.patterns.GitWildMatchPattern, self.options_exclude
        )

    @property
    @memo
    def include_spec(self) -> PathSpec:
        return PathSpec.from_lines(
            pathspec.patterns.GitWildMatchPattern, self.options_include
        )

    @property
    @memo
    def exclude_compiled_src_spec(self) -> PathSpec:
        return PathSpec.from_lines(
            pathspec.patterns.GitWildMatchPattern, self.options_exclude_compiled_src
        )

    @property
    @memo
    def include_compiled_src_spec(self) -> PathSpec:
        return PathSpec.from_lines(
            pathspec.patterns.GitWildMatchPattern, self.options_include_compiled_src
        )

    def path_is_included(self, relative_path: str) -> bool:
        if self.include_spec is None:  # no cov
            return True
        return self.include_spec.match_file(relative_path)

    def path_is_excluded(self, relative_path: str) -> bool:
        if self.exclude_spec is None:  # no cov
            return False
        return self.exclude_spec.match_file(relative_path)

    def path_is_wanted(self, relative_path: str) -> bool:
        if not self.options.files.explicit_targets:
            return not self.path_is_excluded(relative_path)
        return (self.path_is_included(relative_path) and
                not self.path_is_excluded(relative_path))

    def path_is_included_compiled_src(self, relative_path: str) -> bool:
        if self.include_compiled_src_spec is None:
            return True
        return self.include_compiled_src_spec.match_file(relative_path)

    def path_is_excluded_compiled_src(self, relative_path: str) -> bool:
        if self.exclude_compiled_src_spec is None:
            return False
        return self.exclude_compiled_src_spec.match_file(relative_path)

    def path_is_wanted_excluded_compiled_src(self, relative_path: str) -> bool:
        is_compiled = self.path_is_wanted(relative_path)
        if not self.options.include_all_compiled_src:
            is_excluded = True
        else:
            is_excluded = self.path_is_excluded_compiled_src(relative_path)
        is_included = self.path_is_included_compiled_src(relative_path)
        return is_compiled and is_excluded and not is_included

    @property
    @memo
    def included_files(self):
        included_files = []
        for relative_path in set([f.relative_path for f in self.build_config.builder.recurse_selected_project_files()]):
            if self.path_is_wanted(relative_path):
                if any(relative_path.endswith(ext) for ext in self.templated_extensions):
                    relative_path = os.path.splitext(relative_path)[0]
                if any(relative_path.endswith(ext) for ext in self.precompiled_extensions):
                    included_files.append(relative_path)
        return list(set(included_files))

    @property
    @memo
    def excluded_compiled_src_files(self):
        excluded_compiled_src_files = []
        for relative_path in set([f.relative_path for f in self.build_config.builder.recurse_selected_project_files()]):
            if (any(relative_path.endswith(ext) for ext in self.precompiled_extensions) and
                    self.path_is_wanted_excluded_compiled_src(relative_path)):
                excluded_compiled_src_files.append(relative_path)
        return list(set(excluded_compiled_src_files))

    @property
    def normalized_included_files(self):
        """
        Produces files in posix format
        """
        return [self.normalize_path(f) for f in self.included_files]

    @property
    def normalized_excluded_compiled_src_files(self):
        return [self.normalize_path(f) for f in self.excluded_compiled_src_files]

    def normalize_aliased_filelike(self, path: str):
        # sometimes we end up with a case where non src produces
        # '..example_lib._alias'
        while ".." in path:
            path = path.replace("..", "")
        return path

    @property
    def grouped_included_files(self) -> ListT[ExtensionArg]:
        grouped: DictT[str, set] = {}
        for norm in self.normalized_included_files:
            root, ext = os.path.splitext(norm)
            ok = True
            if ext == ".pxd":
                pyfile = norm.replace(".pxd", ".py")
                if os.path.exists(pyfile):
                    norm = pyfile  # noqa: PLW2901
                else:
                    ok = False
                    self.app.display_warning(f"attempted to use .pxd file without .py file ({norm})")
            if self.is_src:
                root = root.replace("src/", "")
            root = self.normalize_aliased_filelike(root.replace("/", "."))
            alias = self.options.files.matches_alias(root)
            self.app.display_debug(f"check alias {ok} {root} -> {norm} -> {alias}")
            if alias:
                root = alias
                self.app.display_debug(f"aliased {root} -> {norm}")
            if grouped.get(root) and ok:
                grouped[root].add(norm)
            elif ok:
                grouped[root] = {norm}
        return [ExtensionArg(name=key, files=list(files)) for key, files in grouped.items()]

    @property
    def artifacts(self):
        # Match the exact path starting at the project root
        if self.sdist:
            to_distribute_files = list(self.intermediate_files.keys())
        else:
            to_distribute_files = []
            if self.options.compiled_extensions_as_artifacts:
                to_distribute_files.extend(list(self.compiled_files.keys()))
            if self.options.intermediate_extensions_as_artifacts:
                to_distribute_files.extend(list(self.intermediate_files.keys()))
        return [f"/{artifact}" for artifact in to_distribute_files]

    @property
    def excluded(self):
        if self.sdist:
            return []
        else:
            return self.normalized_excluded_compiled_src_files

    @contextmanager
    def get_build_dirs(self):
        with TemporaryDirectory() as temp_dir:
            yield os.path.realpath(temp_dir)

    def get_aliased_path(self, path: str) -> str:
        path_without_src = path
        if self.is_src:
            path_without_src = path.replace("src/", "")
        path_alias_notation = self.normalize_aliased_filelike(path_without_src.replace("/", "."))
        alias = self.options.files.matches_alias(path_alias_notation)
        if alias is not None:
            included_file = alias.replace(".", "/")
            if self.is_src:
                included_file = f"src/{included_file}"
            return included_file
        return path

    def _glob_files(self, extensions: ListStr, except_extra: bool, apply_aliases: bool = False) -> RelAbsPathMap:
        found_files: RelAbsPathMap = {}
        extra = ""
        if except_extra:
            extra = ".*"
        relative_path_patterns = []
        for included_file in self.included_files_without_extension:
            if apply_aliases:
                included_file = self.get_aliased_path(included_file)
            for ext in extensions:
                relative_path_patterns.append(f"{included_file}{extra}{ext}")
        for pattern in relative_path_patterns:
            for path in iglob(os.path.join(self.root, pattern)):
                abs_path = path
                rel_path = os.path.relpath(abs_path, self.root)
                found_files[rel_path] = abs_path
        return found_files

    @property
    @memo
    def included_files_without_extension(self) -> ListStr:
        return [os.path.splitext(f)[0] for f in self.included_files]

    @property
    def precompiled_files(self) -> RelAbsPathMap:
        return self._glob_files(list(self.precompiled_extensions), except_extra=False)

    @property
    def intermediate_files(self) -> RelAbsPathMap:
        return self._glob_files(list(self.intermediate_extensions), except_extra=False)

    @property
    def compiled_files(self) -> RelAbsPathMap:
        return self._glob_files(list(self.compiled_extensions), except_extra=True, apply_aliases=True)

    @property
    def templated_files(self) -> RelAbsPathMap:
        return self._glob_files(list(self.templated_extensions), except_extra=False)

    @property
    def autogenerated_files(self) -> RelAbsPathMap:
        autogenerated_files = {}
        for k, v in self.templated_files.items():
            # remove ending .in (only at the end)
            if k.endswith(".in"):
                k = k[:-3]
            if v.endswith(".in"):
                v = v[:-3]
            if os.path.exists(v):
                autogenerated_files[k] = v
        return autogenerated_files

    @property
    def inclusion_map(self):
        include = {}
        for compl in list(self.compiled_files.keys()):
            include[compl] = compl
        self.app.display_debug("Derived inclusion map")
        self.app.display_debug(include)
        return include

    def clean(self, versions: ListStr) -> None:
        files_to_remove = list(self.autogenerated_files.values()) + list(self.intermediate_files.values()) + list(self.compiled_files.values())
        self.app.display_info(f"Hatch-cython: Removing {files_to_remove}")
        self.app.display_info(f"Hatch-cython: intermediates {list(self.intermediate_files.values())}")
        for f_as_abs in files_to_remove:
            os.remove(f_as_abs)

    @property
    @memo
    def options(self) -> Config:
        config = parse_from_dict(self)
        if config.compile_py:
            self.precompiled_extensions.add(".py")
        if config.files.explicit_targets:
            self.precompiled_extensions.add(".py")
            self.precompiled_extensions.add(".c")
            self.precompiled_extensions.add(".cc")
            self.precompiled_extensions.add(".cpp")
        return config

    @property
    def sdist(self) -> bool:
        return self.target_name == "sdist"

    @property
    def wheel(self) -> bool:
        return self.target_name == "wheel"

    def build_ext(self):
        with self.get_build_dirs() as temp:
            self.render_templates()

            shared_temp_build_dir = os.path.join(temp, "build")
            temp_build_dir = os.path.join(temp, "tmp")

            os.mkdir(shared_temp_build_dir)
            os.mkdir(temp_build_dir)

            self.app.display_info("Building c/c++ extensions...")
            self.app.display_info(str(self.normalized_included_files))
            setup_file = os.path.join(temp, "setup.py")
            with open(setup_file, "w") as f:
                setup = setup_py(
                    *self.grouped_included_files,
                    options=self.options,
                    sdist=self.sdist,
                )
                self.app.display_debug(setup)
                f.write(setup)

            self.options.validate_include_opts()

            process = subprocess.run(  # noqa: PLW1510
                [  # noqa: S603
                    sys.executable,
                    setup_file,
                    "build_ext",
                    "--inplace",
                    "--verbose",
                    "--build-lib",
                    shared_temp_build_dir,
                    "--build-temp",
                    temp_build_dir,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.options.envflags.env,
            )
            stdout = process.stdout.decode("utf-8")
            if process.returncode:
                self.app.display_error(f"cythonize exited non null status {process.returncode}")
                self.app.display_error(stdout)
                msg = "failed compilation"
                raise Exception(msg)
            else:
                self.app.display_info(stdout)

            self.app.display_success("Post-build artifacts")

    def initialize(self, version: str, build_data: dict):
        self.app.display_mini_header(self.PLUGIN_NAME)
        self.app.display_debug("options")
        self.app.display_debug(self.options.asdict(), level=1)
        self.app.display_debug("sdist")
        self.app.display_debug(self.sdist, level=1)
        self.app.display_waiting("pre-build artifacts")

        if len(self.grouped_included_files) != 0:
            self.build_ext()
            self.app.display_info(glob(f"{self.project_dir}/*/**", recursive=True))

        if self.sdist and not self.options.compiled_sdist:
            self.clean(None)

        build_data["infer_tag"] = True
        build_data["artifacts"].extend(self.artifacts)
        # compiled extensions are also in force_include so their paths can be modified
        build_data["force_include"].update(self.inclusion_map)
        build_data["pure_python"] = False
        if len(self.excluded) > 0:
            if "exclude" not in self.build_config.target_config:
                self.build_config.target_config["exclude"] = []
            self.build_config.target_config["exclude"].extend(
                [remove_leading_dot(f) for f in self.excluded] + ['src/dataspree/inspection/features/*.py']
            )
        self.app.display_debug(f"Hook Config: {self.config}")
        self.app.display_debug(f"Build config: {self.build_config.build_config}")
        self.app.display_debug(f"Target config: {self.build_config.target_config}")

        self.app.display_info("Extensions complete")
        self.app.display_debug(f"Build data: {build_data}")
