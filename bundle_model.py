"""Read a version with specklepy's bundle API (receive3) and project it two ways: an element
index for the tools, and a Base tree for @speckle/viewer.

Built from the bundle Model rather than Model.to_base(): to_base drops instanced objects' data,
objects without display geometry and instanced-geometry materials, and duplicates definition
applicationIds (ENG-11569). The relation maps read off `model.bundle.relations` are the ones
specklepy's own projection uses; the specklepy pin in plugin.yaml keeps them stable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import speckle_api
from .speckle_api import ModelRef

IDENTITY = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0]
_SCALARS = (str, int, float, bool, type(None))


class UnsupportedVersion(LookupError):
    pass


def receive(ref: ModelRef, version: Any):
    """The version as a specklepy bundle Model; the caller closes it."""
    if not speckle_api.is_bundle(version):
        raise UnsupportedVersion(
            f"Version {version.id} predates Speckle's bundle format. This plugin reads bundle "
            "versions only; publish the model again from a current Speckle connector."
        )
    from specklepy.api import operations

    return operations.receive3(
        speckle_api.client().account, ref.project_id, ref.model_id, version.id,
        include_geometry=True, mark_received=False,
    )


# ── element index ────────────────────────────────────────────────────────────


def _level_by_object(model) -> dict[int, str]:
    return {obj.k: level.name for level in model.levels for obj in level.objects if level.name}


def _definition_signatures(model) -> dict[int, str]:
    """A content hash per definition, so an edit to a family's geometry changes every instance's
    fingerprint even though the instances' own properties did not change."""
    rels = model.bundle.relations
    geometries = model.geometries
    signatures: dict[int, str] = {}
    for definition in model.definitions:
        h = hashlib.sha1()
        for geometry_k in rels.defines_by_definition.get(definition.k, []):
            geometry = geometries.get(geometry_k)
            if geometry is not None:
                h.update(hashlib.sha1(geometry.content).digest())
        signatures[definition.k] = h.hexdigest()
    return signatures


def _record(obj, level: str | None, definition_signatures: dict[int, str], model) -> dict:
    category = obj.get_string("category") or "Uncategorized"
    props: dict[str, Any] = {
        "category": category,
        "family": obj.get_string("family"),
        "type": obj.get_string("type"),
        "level": level,
    }
    for source in (obj.type_properties, obj.properties):
        props.update({k: v for k, v in source.items() if isinstance(v, _SCALARS)})

    geometry = hashlib.sha1()
    for edge in model.bundle.relations.display_by_object(obj.k):
        content = model.geometries.get(edge.dst)
        if content is not None:
            geometry.update(hashlib.sha1(content.content).digest())
    for placement in obj.placements:
        definition = placement.definition
        geometry.update(json.dumps(placement.transform).encode())
        geometry.update(definition_signatures.get(definition.k, "").encode() if definition else b"")

    fingerprint = hashlib.sha1(
        json.dumps({"p": props, "n": obj.name}, sort_keys=True, default=str).encode() + geometry.digest()
    ).hexdigest()
    return {
        "key": obj.application_id,
        "label": category,
        "name": obj.name or "",
        "fp": fingerprint,
        "props": props,
    }


def element_index(model) -> dict[str, dict]:
    levels = _level_by_object(model)
    signatures = _definition_signatures(model)
    return {obj.application_id: _record(obj, levels.get(obj.k), signatures, model) for obj in model.objects}


# ── viewer tree ──────────────────────────────────────────────────────────────


def _deep_merge(into: dict, other: dict) -> dict:
    for k, v in other.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _deep_merge(into[k], v)
        else:
            into[k] = v
    return into


class _Appearance:
    """RenderMaterialProxy / ColorProxy per bundle node, targeting viewer applicationIds."""

    def __init__(self, model) -> None:
        self._model = model
        self.materials: dict[int, list[str]] = {}
        self.colors: dict[int, list[str]] = {}

    def assign(self, target_id: str, material, color) -> None:
        if material is not None:
            self.materials.setdefault(material.k, []).append(target_id)
        if color is not None:
            self.colors.setdefault(color.k, []).append(target_id)

    def proxies(self) -> tuple[list, list]:
        from specklepy.objects.other import RenderMaterial
        from specklepy.objects.proxies import ColorProxy, RenderMaterialProxy

        materials = []
        for k, targets in self.materials.items():
            node = self._model.node(k)
            fields = node.fields
            value = RenderMaterial(
                name=node.name or "material", diffuse=fields.argb, opacity=fields.opacity,
                metalness=fields.metalness, roughness=fields.roughness, emissive=fields.emissive or 0,
            )
            value.applicationId = f"mat-{k}"
            proxy = RenderMaterialProxy(value=value, objects=targets)
            proxy.applicationId = f"matproxy-{k}"
            materials.append(proxy)
        colors = []
        for k, targets in self.colors.items():
            proxy = ColorProxy(value=self._model.node(k).fields.argb, objects=targets)
            proxy.applicationId = f"colproxy-{k}"
            colors.append(proxy)
        return materials, colors


def _node(model, k: int | None):
    return model.node(k) if k is not None else None


def _definition_depths(model) -> dict[int, int]:
    rels = model.bundle.relations
    nested = rels.defines_instance_by_definition
    depth: dict[int, int] = {}

    def propagate(definition_k: int, d: int, on_stack: set[int]) -> None:
        if definition_k in on_stack or depth.get(definition_k, -1) >= d:
            return
        depth[definition_k] = d
        on_stack.add(definition_k)
        for instance_k in nested.get(definition_k, []):
            instance = model.node(instance_k)
            if instance is not None and instance.definition is not None:
                propagate(instance.definition.k, d + 1, on_stack)
        on_stack.discard(definition_k)

    for edge in rels.display_instance_edges:
        instance = model.node(edge.dst)
        if instance is not None and instance.definition is not None:
            propagate(instance.definition.k, 0, set())
    return depth


def _instance_proxy(app_id: str, instance, units: str, depth: int):
    from specklepy.objects.proxies import InstanceProxy

    definition = instance.definition
    proxy = InstanceProxy(
        definitionId=f"def-{definition.k if definition is not None else -1}",
        transform=instance.transform or list(IDENTITY),
        maxDepth=depth,
        units=instance.units or units,
    )
    proxy.applicationId = app_id
    return proxy


def _mesh(model, geometry_k: int, app_id: str):
    from specklepy.bundle import sgeo

    geometry = model.geometries.get(geometry_k)
    if geometry is None or not geometry.is_sgeo:
        return None
    decoded = sgeo.decode(geometry.content)
    decoded.applicationId = app_id
    return decoded


def viewer_tree(model, name: str):
    """A v3-connector-shaped tree: Collections by collection path, one DataObject per element
    carrying its properties, display meshes or InstanceProxy placements, definitions and
    material/color proxies. Every applicationId is unique."""
    from specklepy.objects.data_objects import DataObject
    from specklepy.objects.models.collections.collection import Collection
    from specklepy.objects.proxies import InstanceDefinitionProxy

    rels = model.bundle.relations
    units = model.units or "m"
    appearance = _Appearance(model)
    depths = _definition_depths(model)

    root = Collection(name=name, elements=[])
    root.applicationId = "root"
    collections: dict[tuple[str, ...], Any] = {}

    def collection_for(path: list[str]):
        parent, key = root, ()
        for part in path:
            key = key + (part,)
            if key not in collections:
                child = Collection(name=part, elements=[])
                child.applicationId = "coll-" + hashlib.sha1("/".join(key).encode()).hexdigest()[:16]
                parent.elements.append(child)
                collections[key] = child
            parent = collections[key]
        return parent

    for obj in model.objects:
        display: list[Any] = []
        for edge in rels.display_by_object(obj.k):
            mesh = _mesh(model, edge.dst, f"geo-{edge.dst}")
            if mesh is not None:
                display.append(mesh)
                geometry_material = _node(model, rels.material_by_geometry.get(edge.dst))
                geometry_color = _node(model, rels.color_by_geometry.get(edge.dst))
                appearance.assign(
                    mesh.applicationId,
                    geometry_material or obj.material or obj.container_material,
                    obj.color or geometry_color or obj.container_color,
                )
        for placement in obj.placements:
            depth = depths.get(placement.definition.k, 0) if placement.definition else 0
            display.append(_instance_proxy(f"inst-{obj.k}-{placement.k}", placement, units, depth))

        properties = _deep_merge(obj.type_properties.to_nested(), obj.properties.to_nested())
        element = DataObject(name=obj.name or "element", properties=properties, displayValue=display)
        element.applicationId = obj.application_id
        for field in ("category", "family", "type"):
            value = obj.get_string(field)
            if value:
                element[field] = value
        collection_for(obj.collection_path or [obj.get_string("category") or "Elements"]).elements.append(element)

    definitions_collection = Collection(name="Definitions", elements=[])
    definitions_collection.applicationId = "definitions"
    definition_proxies = []
    for definition in model.definitions:
        members: list[str] = []
        owners = definition.objects
        owner = owners[0] if owners else None
        for geometry_k in rels.defines_by_definition.get(definition.k, []):
            mesh = _mesh(model, geometry_k, f"defgeo-{geometry_k}")
            if mesh is None:
                continue
            definitions_collection.elements.append(mesh)
            members.append(mesh.applicationId)
            geometry_material = _node(model, rels.material_by_geometry.get(geometry_k))
            geometry_color = _node(model, rels.color_by_geometry.get(geometry_k))
            appearance.assign(
                mesh.applicationId,
                geometry_material or (owner.material if owner else None) or (owner.container_material if owner else None),
                geometry_color or (owner.color if owner else None),
            )
        for instance_k in rels.defines_instance_by_definition.get(definition.k, []):
            instance = model.node(instance_k)
            if instance is None:
                continue
            nested = _instance_proxy(f"nested-{definition.k}-{instance_k}", instance, units, 0)
            definitions_collection.elements.append(nested)
            members.append(nested.applicationId)
        proxy = InstanceDefinitionProxy(
            name=definition.name or f"Definition {definition.k}",
            objects=members,
            maxDepth=depths.get(definition.k, 0),
        )
        proxy.applicationId = f"def-{definition.k}"
        definition_proxies.append(proxy)

    if definitions_collection.elements:
        root.elements.append(definitions_collection)
    if definition_proxies:
        root["instanceDefinitionProxies"] = definition_proxies
    materials, colors = appearance.proxies()
    if materials:
        root["renderMaterialProxies"] = materials
    if colors:
        root["colorProxies"] = colors
    root["units"] = units
    return root
