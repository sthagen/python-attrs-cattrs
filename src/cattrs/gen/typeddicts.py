from __future__ import annotations

import linecache
import re
from inspect import get_annotations
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Type, TypeVar

from attr import NOTHING, Attribute

from .._compat import get_origin, is_annotated, is_bare, is_generic
from .._generics import deep_copy_with
from ..errors import ClassValidationError, StructureHandlerNotFoundError
from . import AttributeOverride
from ._consts import already_generating, neutral
from ._generics import generate_mapping
from ._lc import generate_unique_filename
from ._shared import find_structure_handler

if TYPE_CHECKING:  # pragma: no cover
    from cattr.converters import BaseConverter

T = TypeVar("T")


def make_dict_unstructure_fn(
    cl: Type[T],
    converter: BaseConverter,
    _cattrs_use_linecache: bool = True,
    **kwargs: AttributeOverride,
) -> Callable[[T], Dict[str, Any]]:
    """
    Generate a specialized dict unstructuring function for a TypedDict.
    """
    origin = get_origin(cl)
    attrs = adapted_fields(origin or cl)  # type: ignore
    req_keys = (origin or cl).__required_keys__

    mapping = {}
    if is_generic(cl):
        mapping = generate_mapping(cl, mapping)

        for base in getattr(origin, "__orig_bases__", ()):
            if is_generic(base) and not str(base).startswith("typing.Generic"):
                mapping = generate_mapping(base, mapping)
                break

        # It's possible for origin to be None if this is a subclass
        # of a generic class.
        if origin is not None:
            cl = origin

    cl_name = cl.__name__
    fn_name = "unstructure_typeddict_" + cl_name
    globs = {}
    lines = []
    internal_arg_parts = {}

    # We keep track of what we're generating to help with recursive
    # class graphs.
    try:
        working_set = already_generating.working_set
    except AttributeError:
        working_set = set()
        already_generating.working_set = working_set
    if cl in working_set:
        raise RecursionError()
    else:
        working_set.add(cl)

    try:
        # We want to short-circuit in certain cases and return the identity
        # function.
        # We short-circuit if all of these are true:
        # * no attributes have been overridden
        # * all attributes resolve to `converter._unstructure_identity`
        for a in attrs:
            attr_name = a.name
            override = kwargs.get(attr_name, neutral)
            if override != neutral:
                break
            handler = None
            t = a.type

            if isinstance(t, TypeVar):
                if t.__name__ in mapping:
                    t = mapping[t.__name__]
                else:
                    handler = converter.unstructure
            elif is_generic(t) and not is_bare(t) and not is_annotated(t):
                t = deep_copy_with(t, mapping)

            if handler is None:
                try:
                    handler = converter._unstructure_func.dispatch(t)
                except RecursionError:
                    # There's a circular reference somewhere down the line
                    handler = converter.unstructure
            is_identity = handler == converter._unstructure_identity
            if not is_identity:
                break
        else:
            # We've not broken the loop.
            return converter._unstructure_identity

        for a in attrs:
            print(a)
            attr_name = a.name
            override = kwargs.get(attr_name, neutral)
            if override.omit:
                continue
            kn = attr_name if override.rename is None else override.rename
            attr_required = attr_name in req_keys

            # For each attribute, we try resolving the type here and now.
            # If a type is manually overwritten, this function should be
            # regenerated.
            handler = None
            if override.unstruct_hook is not None:
                handler = override.unstruct_hook
            else:
                t = a.type
                if isinstance(t, TypeVar):
                    if t.__name__ in mapping:
                        t = mapping[t.__name__]
                    else:
                        handler = converter.unstructure
                elif is_generic(t) and not is_bare(t) and not is_annotated(t):
                    t = deep_copy_with(t, mapping)

                if handler is None:
                    try:
                        handler = converter._unstructure_func.dispatch(t)
                    except RecursionError:
                        # There's a circular reference somewhere down the line
                        handler = converter.unstructure

            is_identity = handler == converter._unstructure_identity

            if not is_identity:
                unstruct_handler_name = f"__c_unstr_{attr_name}"
                globs[unstruct_handler_name] = handler
                internal_arg_parts[unstruct_handler_name] = handler
                invoke = f"{unstruct_handler_name}(instance['{attr_name}'])"
            else:
                # We're not doing anything to this attribute, so
                # it'll already be present in the input dict.
                continue

            if attr_required:
                # No default or no override.
                lines.append(f"  res['{kn}'] = {invoke}")
            else:
                lines.append(f"  if '{kn}' in instance: res['{kn}'] = {invoke}")

        internal_arg_line = ", ".join([f"{i}={i}" for i in internal_arg_parts])
        if internal_arg_line:
            internal_arg_line = f", {internal_arg_line}"
        for k, v in internal_arg_parts.items():
            globs[k] = v

        total_lines = (
            [f"def {fn_name}(instance{internal_arg_line}):"]
            + ["  res = instance.copy()"]
            + lines
            + ["  return res"]
        )
        script = "\n".join(total_lines)

        fname = generate_unique_filename(
            cl, "unstructure", reserve=_cattrs_use_linecache
        )

        eval(compile(script, fname, "exec"), globs)

        fn = globs[fn_name]
        if _cattrs_use_linecache:
            linecache.cache[fname] = len(script), None, total_lines, fname
    finally:
        working_set.remove(cl)

    return fn


def make_dict_structure_fn(
    cl: Any,
    converter: BaseConverter,
    _cattrs_use_linecache: bool = True,
    _cattrs_detailed_validation: bool = True,
    **kwargs: AttributeOverride,
) -> Callable[[Dict, Any], Any]:
    """Generate a specialized dict structuring function for typed dicts."""

    mapping = {}
    if is_generic(cl):
        base = get_origin(cl)
        mapping = generate_mapping(cl, mapping)
        if base is not None:
            # It's possible for this to be a subclass of a generic,
            # so no origin.
            cl = base

    for base in getattr(cl, "__orig_bases__", ()):
        if is_generic(base) and not str(base).startswith("typing.Generic"):
            mapping = generate_mapping(base, mapping)
            break

    if isinstance(cl, TypeVar):
        cl = mapping.get(cl.__name__, cl)

    cl_name = cl.__name__
    fn_name = "structure_" + cl_name
    req_keys = cl.__required_keys__

    # We have generic parameters and need to generate a unique name for the function
    for p in getattr(cl, "__parameters__", ()):
        # This is nasty, I am not sure how best to handle `typing.List[str]` or `TClass[int, int]` as a parameter type here
        try:
            name_base = mapping[p.__name__]
        except KeyError:
            raise StructureHandlerNotFoundError(
                f"Missing type for generic argument {p.__name__}, specify it when structuring.",
                p,
            ) from None
        name = getattr(name_base, "__name__", None) or str(name_base)
        # `<>` can be present in lambdas
        # `|` can be present in unions
        name = re.sub(r"[\[\.\] ,<>]", "_", name)
        name = re.sub(r"\|", "u", name)
        fn_name += f"_{name}"

    internal_arg_parts = {"__cl": cl}
    globs = {}
    lines = []
    post_lines = []

    attrs = adapted_fields(cl)

    allowed_fields = set()
    # if _cattrs_forbid_extra_keys:
    #     globs["__c_a"] = allowed_fields
    #     globs["__c_feke"] = ForbiddenExtraKeysError

    lines.append("  res = o.copy()")

    if _cattrs_detailed_validation:
        lines.append("  errors = []")
        internal_arg_parts["__c_cve"] = ClassValidationError
        for a in attrs:
            an = a.name
            attr_required = an in req_keys
            override = kwargs.get(an, neutral)
            if override.omit:
                continue
            t = a.type
            if isinstance(t, TypeVar):
                t = mapping.get(t.__name__, t)
            elif is_generic(t) and not is_bare(t) and not is_annotated(t):
                t = deep_copy_with(t, mapping)

            # For each attribute, we try resolving the type here and now.
            # If a type is manually overwritten, this function should be
            # regenerated.
            if override.struct_hook is not None:
                # If the user has requested an override, just use that.
                handler = override.struct_hook
            else:
                handler = find_structure_handler(a, t, converter)

            struct_handler_name = f"__c_structure_{an}"
            internal_arg_parts[struct_handler_name] = handler

            kn = an if override.rename is None else override.rename
            allowed_fields.add(kn)
            i = "  "
            if not attr_required:
                lines.append(f"{i}if '{kn}' in o:")
                i = f"{i}  "
            lines.append(f"{i}try:")
            i = f"{i}  "
            if handler:
                if handler == converter._structure_call:
                    internal_arg_parts[struct_handler_name] = t
                    lines.append(f"{i}res['{an}'] = {struct_handler_name}(o['{kn}'])")
                else:
                    type_name = f"__c_type_{an}"
                    internal_arg_parts[type_name] = t
                    lines.append(
                        f"{i}res['{an}'] = {struct_handler_name}(o['{kn}'], {type_name})"
                    )
            else:
                lines.append(f"{i}res['{an}'] = o['{kn}']")
            i = i[:-2]
            lines.append(f"{i}except Exception as e:")
            i = f"{i}  "
            lines.append(
                f"{i}e.__notes__ = getattr(e, '__notes__', []) + [\"Structuring class {cl.__qualname__} @ attribute {an}\"]"
            )
            lines.append(f"{i}errors.append(e)")

        # if _cattrs_forbid_extra_keys:
        #     post_lines += [
        #         "  unknown_fields = set(o.keys()) - __c_a",
        #         "  if unknown_fields:",
        #         "    errors.append(__c_feke('', __cl, unknown_fields))",
        #     ]

        post_lines.append(
            f"  if errors: raise __c_cve('While structuring ' + {cl.__name__!r}, errors, __cl)"
        )
    else:
        non_required = []

        # The first loop deals with required args.
        for a in attrs:
            an = a.name
            attr_required = an in req_keys
            override = kwargs.get(an, neutral)
            if override.omit:
                continue
            if not attr_required:
                non_required.append(a)
                continue
            t = a.type
            if isinstance(t, TypeVar):
                t = mapping.get(t.__name__, t)
            elif is_generic(t) and not is_bare(t) and not is_annotated(t):
                t = deep_copy_with(t, mapping)

            # For each attribute, we try resolving the type here and now.
            # If a type is manually overwritten, this function should be
            # regenerated.
            if t is not None:
                handler = converter._structure_func.dispatch(t)
            else:
                handler = converter.structure

            kn = an if override.rename is None else override.rename
            allowed_fields.add(kn)

            if handler:
                struct_handler_name = f"__c_structure_{an}"
                internal_arg_parts[struct_handler_name] = handler
                if handler == converter._structure_call:
                    internal_arg_parts[struct_handler_name] = t
                    invocation_line = (
                        f"  res['{kn}'] = {struct_handler_name}(o['{kn}'])"
                    )
                else:
                    type_name = f"__c_type_{an}"
                    internal_arg_parts[type_name] = t
                    invocation_line = (
                        f"  res['{kn}'] = {struct_handler_name}(o['{kn}'], {type_name})"
                    )
            else:
                invocation_line = f"  res['{kn}'] = o['{kn}']"

            lines.append(invocation_line)

        # The second loop is for optional args.
        if non_required:
            for a in non_required:
                an = a.name
                override = kwargs.get(an, neutral)
                t = a.type
                if isinstance(t, TypeVar):
                    t = mapping.get(t.__name__, t)
                elif is_generic(t) and not is_bare(t) and not is_annotated(t):
                    t = deep_copy_with(t, mapping)

                # For each attribute, we try resolving the type here and now.
                # If a type is manually overwritten, this function should be
                # regenerated.
                if t is not None:
                    handler = converter._structure_func.dispatch(t)
                else:
                    handler = converter.structure

                struct_handler_name = f"__c_structure_{an}"
                internal_arg_parts[struct_handler_name] = handler

                ian = an
                kn = an if override.rename is None else override.rename
                allowed_fields.add(kn)
                post_lines.append(f"  if '{kn}' in o:")
                if handler:
                    if handler == converter._structure_call:
                        internal_arg_parts[struct_handler_name] = t
                        post_lines.append(
                            f"    res['{ian}'] = {struct_handler_name}(o['{kn}'])"
                        )
                    else:
                        type_name = f"__c_type_{an}"
                        internal_arg_parts[type_name] = t
                        post_lines.append(
                            f"    res['{ian}'] = {struct_handler_name}(o['{kn}'], {type_name})"
                        )
                else:
                    post_lines.append(f"    res['{ian}'] = o['{kn}']")

        # if _cattrs_forbid_extra_keys:
        #     post_lines += [
        #         "  unknown_fields = set(o.keys()) - __c_a",
        #         "  if unknown_fields:",
        #         "    raise __c_feke('', __cl, unknown_fields)",
        #     ]

    # At the end, we create the function header.
    internal_arg_line = ", ".join([f"{i}={i}" for i in internal_arg_parts])
    for k, v in internal_arg_parts.items():
        globs[k] = v

    total_lines = (
        [f"def {fn_name}(o, _, *, {internal_arg_line}):"]
        + lines
        + post_lines
        + ["  return res"]
    )

    fname = generate_unique_filename(cl, "structure", reserve=_cattrs_use_linecache)
    script = "\n".join(total_lines)
    eval(compile(script, fname, "exec"), globs)
    if _cattrs_use_linecache:
        linecache.cache[fname] = len(script), None, total_lines, fname

    return globs[fn_name]


def adapted_fields(cls: Any) -> List[Attribute]:
    annotations = get_annotations(cls, eval_str=True)
    return [
        Attribute(n, NOTHING, None, False, False, False, False, False, type=a)
        for n, a in annotations.items()
    ]
