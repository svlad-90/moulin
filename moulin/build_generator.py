# SPDX-License-Identifier: Apache-2.0
# Copyright 2021 EPAM Systems
"""
This module takes processed YAML config tree (with variables expanded
and parameters applied) and produces Ninja build file
"""

import os.path
import sys
from typing import Optional, List

from importlib import import_module
from moulin import ninja_syntax
from moulin import make_syntax
from moulin import rouge
from moulin import yaml_helpers as yh
from moulin.yaml_helpers import YAMLProcessingError
from moulin.build_conf import MoulinConfiguration

BUILD_FILENAME = 'build.ninja'
DEPENDENCY_POLICY_FETCHED = "fetched_files"
DEPENDENCY_POLICY_BUILD = "build_files"
DEPENDENCY_POLICY_ALL = "all_files"
DEPENDENCY_POLICIES = [
    DEPENDENCY_POLICY_FETCHED,
    DEPENDENCY_POLICY_BUILD,
    DEPENDENCY_POLICY_ALL,
]


def generate_build(conf: MoulinConfiguration,
                   conf_file_name,
                   ninja_build_fname=BUILD_FILENAME) -> None:
    """
    Write Ninja build file based on pre-processed config tree
    """
    _flatten_sources(conf)
    # Validate dependency tracking before touching build.ninja, so an invalid
    # policy cannot leave a partially generated build file behind.
    builder_modules, fetcher_modules = _get_modules(conf, None)
    _validate_dependency_configuration(conf, builder_modules, fetcher_modules)

    generator = ninja_syntax.Writer(open(ninja_build_fname, 'w'), width=120)

    generator.variable("ninja_required_version", "1.10")

    _gen_regenerate(conf_file_name, generator)

    rouge.gen_build_rules(generator)

    # We want to have all Ninja build rules before all actual build
    # commands. So we need to scan conf twice. On the first scan we will
    # determine and load all required plugins. On the same time, we'll ask them
    # to generate Ninja rules.
    builder_modules, fetcher_modules = _get_modules(conf, generator)

    root = conf.get_root()
    # Now we have all plugins loaded and we can generate some build rules
    for comp_name, component in root["components"].items():
        build_dir = component.get("build-dir", comp_name).as_str
        builder_conf = component["builder"]

        source_stamps = []
        if "sources" in component:
            for source in component["sources"]:
                source_type = source["type"].as_str
                fetcher_module = fetcher_modules[source_type]
                fetcher = fetcher_module.get_fetcher(source, build_dir, generator)
                fetcher_stamps = fetcher.gen_fetch()
                if isinstance(fetcher_stamps, list):
                    source_stamps.extend(fetcher_stamps)
                else:
                    source_stamps.append(fetcher_stamps)

        # Generate handy 'fetch-{component}' rule
        generator.build(f"fetch-{comp_name}", "phony", source_stamps)
        generator.newline()

        builder_type = builder_conf["type"].as_str
        builder_module = builder_modules[builder_type]
        builder = builder_module.get_builder(builder_conf, comp_name, build_dir, source_stamps,
                                             generator)

        build_stamps = builder.gen_build()
        generator.build(comp_name, "phony", build_stamps)
        generator.newline()
        if component.get("default", False).as_bool:
            generator.default(comp_name)

    rouge.gen_build(generator, rouge.get_available_images(conf.get_root()))


def generate_fetcher_dyndep(conf: MoulinConfiguration, component: str):
    _flatten_sources(conf)

    deps_context = _get_dependency_context(conf, component)
    deps = _get_fetcher_file_list(deps_context)
    _write_dyndep(component, deps_context["targets"], deps)


def generate_component_dyndep(conf: MoulinConfiguration, component: str):
    _flatten_sources(conf)

    deps_context = _get_dependency_context(conf, component)
    policy = _get_dependency_policy(deps_context["component_node"])

    if policy in [DEPENDENCY_POLICY_BUILD, DEPENDENCY_POLICY_ALL]:
        _ensure_build_file_support(deps_context["builder"],
                                   deps_context["builder_type"],
                                   deps_context["component_node"])

    deps: List[str] = []
    if policy in [DEPENDENCY_POLICY_FETCHED, DEPENDENCY_POLICY_ALL]:
        deps.extend(_get_fetcher_file_list(deps_context))
    if policy in [DEPENDENCY_POLICY_BUILD, DEPENDENCY_POLICY_ALL]:
        deps.extend(_get_builder_file_list(deps_context))
    _write_dyndep(component, deps_context["targets"], deps)


def _get_dependency_context(conf: MoulinConfiguration, component: str):
    builder_modules, fetcher_modules = _get_modules(conf, None)
    component_node = conf.get_root()["components"][component]
    build_dir = component_node.get("build-dir", component).as_str
    builder_node = component_node["builder"]
    builder_type = builder_node["type"].as_str
    builder_module = builder_modules[builder_type]
    # Dependency-only mode does not generate Ninja rules. Builders that provide
    # dependency metadata must keep that path independent from rule generation.
    builder = builder_module.get_builder(builder_node, component, build_dir, [], None)

    targets = builder.get_targets()
    return {
        "build_dir": build_dir,
        "component_node": component_node,
        "builder_type": builder_type,
        "fetcher_modules": fetcher_modules,
        "builder": builder,
        "targets": targets,
    }


def _get_fetcher_file_list(deps_context) -> List[str]:
    deps: List[str] = []
    component_node = deps_context["component_node"]
    if "sources" in component_node:
        for source in component_node["sources"]:
            source_type = source["type"].as_str
            fetcher_module = deps_context["fetcher_modules"][source_type]
            # Dependency-only mode does not generate Ninja rules. Fetchers that
            # expose get_file_list must make that method independent from generator.
            fetcher = fetcher_module.get_fetcher(source, deps_context["build_dir"], None)
            # Keep this as a runtime guard for direct internal --dep calls.
            _ensure_fetcher_file_support(fetcher, source_type, source)
            deps.extend(fetcher.get_file_list())
    return deps


def _get_builder_file_list(deps_context) -> List[str]:
    _ensure_build_file_support(deps_context["builder"],
                               deps_context["builder_type"],
                               deps_context["component_node"])
    return deps_context["builder"].get_build_file_list()


def _ensure_build_file_support(builder, builder_type: str, component_node) -> None:
    get_build_file_list = getattr(builder, "get_build_file_list", None)
    if not get_build_file_list:
        raise YAMLProcessingError(
            f"Builder '{builder_type}' does not support "
            "dependency_policy values that require build files",
            component_node.mark)


def _ensure_fetcher_file_support(fetcher, fetcher_type: str, source_node) -> None:
    get_file_list = getattr(fetcher, "get_file_list", None)
    if not get_file_list:
        raise YAMLProcessingError(
            f"Fetcher '{fetcher_type}' does not support "
            "dependency_policy values that require fetched files",
            source_node.mark)


def _validate_dependency_configuration(conf: MoulinConfiguration,
                                       builder_modules,
                                       fetcher_modules) -> None:
    root = conf.get_root()
    for component_name, component_node in root["components"].items():
        build_dir = component_node.get("build-dir", component_name).as_str
        builder_node = component_node["builder"]
        builder_type = builder_node["type"].as_str
        builder_module = builder_modules[builder_type]
        # Dependency validation only needs metadata. Builders that expose
        # get_build_file_list must make that method independent from generator.
        builder = builder_module.get_builder(builder_node, component_name, build_dir, [], None)
        deps_context = {
            "build_dir": build_dir,
            "component_node": component_node,
            "builder_type": builder_type,
            "fetcher_modules": fetcher_modules,
            "builder": builder,
            "targets": builder.get_targets(),
        }
        policy = _get_dependency_policy(component_node)
        if policy in [DEPENDENCY_POLICY_BUILD, DEPENDENCY_POLICY_ALL]:
            _ensure_build_file_support(builder, builder_type, component_node)
        if policy in [DEPENDENCY_POLICY_FETCHED, DEPENDENCY_POLICY_ALL]:
            _ensure_fetcher_file_support_for_component(deps_context)


def _ensure_fetcher_file_support_for_component(deps_context) -> None:
    component_node = deps_context["component_node"]
    if "sources" not in component_node:
        return
    for source in component_node["sources"]:
        source_type = source["type"].as_str
        fetcher_module = deps_context["fetcher_modules"][source_type]
        # Dependency validation only needs metadata. Fetchers that expose
        # get_file_list must make that method independent from generator.
        fetcher = fetcher_module.get_fetcher(source, deps_context["build_dir"], None)
        _ensure_fetcher_file_support(fetcher, source_type, source)


def _get_dependency_policy(component_node) -> str:
    policy_node = component_node.get("dependency_policy", DEPENDENCY_POLICY_FETCHED)
    policy = policy_node.as_str
    if policy not in DEPENDENCY_POLICIES:
        raise YAMLProcessingError(
            f"Unsupported dependency_policy '{policy}'. "
            f"Expected one of: {', '.join(DEPENDENCY_POLICIES)}",
            policy_node.mark)
    return policy


def _write_dyndep(component: str, targets: List[str], deps: List[str]) -> None:
    with open(f".moulin_{component}.d", 'w') as stream:
        generator = make_syntax.Writer(stream, width=120)
        generator.simple_dep(targets, sorted(set(deps)))


def _gen_regenerate(conf_file_name, generator: ninja_syntax.Writer):
    this_script = os.path.basename(sys.argv[0])
    args = " ".join(sys.argv[1:])
    generator.rule("regenerate", command=f"{this_script} {args}", generator=1)
    generator.newline()
    generator.build(BUILD_FILENAME, "regenerate", conf_file_name)
    generator.newline()


def _flatten_sources(conf: MoulinConfiguration):
    for _, component in yh.get_mandatory_mapping(conf.get_root_node(), "components"):
        if yh.get_node(component, "sources"):
            yh.flatten_list(yh.get_mandatory_sequence_node(component, "sources"))


def _get_modules(conf: MoulinConfiguration, generator: Optional[ninja_syntax.Writer]):
    builder_modules = {}
    fetcher_modules = {}
    for _, component in conf.get_root()["components"].items():
        b_type = component["builder"]["type"].as_str
        if b_type not in builder_modules:
            builder_modules[b_type] = _prepare_builder(b_type, generator)
        if "sources" in component:
            for source in component["sources"]:
                f_type = source["type"].as_str
                if f_type not in fetcher_modules:
                    fetcher_modules[f_type] = _prepare_fetcher(f_type, generator)
    return builder_modules, fetcher_modules


def _prepare_builder(builder, generator):
    module = import_module(f".builders.{builder}", __package__)
    if generator:
        module.gen_build_rules(generator)
    return module


def _prepare_fetcher(fetcher, generator):
    module = import_module(f".fetchers.{fetcher}", __package__)
    if generator:
        module.gen_build_rules(generator)
    return module
