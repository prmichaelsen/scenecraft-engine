"""Ingredients router (M16 T60).

Thin wrappers over the ingredients.json-based CRUD in
``api_server.py::_handle_*_ingredient*``.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends

from scenecraft.api.deps import current_user, project_dir as project_dir_dep
from scenecraft.api.errors import ApiError
from scenecraft.api.models.projects import (
    IngredientsPromoteBody,
    IngredientsRemoveBody,
    IngredientsUpdateBody,
)


router = APIRouter(tags=["ingredients"], dependencies=[Depends(current_user)])


def _log(msg: str) -> None:
    from scenecraft.api_server import _log as legacy_log

    legacy_log(msg)


@router.get(
    "/api/projects/{name}/ingredients",
    operation_id="list_ingredients",
    summary="List ingredient images for a project",
)
async def list_ingredients(
    name: str, proj: Path = Depends(project_dir_dep)
) -> dict:
    try:
        ing_file = proj / "ingredients.json"
        if ing_file.exists():
            data = json.loads(ing_file.read_text())
            return {"ingredients": data.get("ingredients", [])}
        return {"ingredients": []}
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/ingredients/promote",
    operation_id="promote_ingredient",
    summary="Copy a source image into the ingredients pool",
)
async def promote_ingredient(
    name: str,
    body: IngredientsPromoteBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    try:
        source_type = body.sourceType or "keyframe"
        source_path = body.sourcePath or ""
        label = body.label or ""
        src = proj / source_path
        if not src.exists():
            raise ApiError(
                "NOT_FOUND", f"Source not found: {source_path}", status_code=404
            )
        ing_dir = proj / "ingredients"
        ing_dir.mkdir(parents=True, exist_ok=True)
        ing_id = f"ing_{uuid.uuid4().hex[:8]}"
        ext = src.suffix or ".png"
        dest = ing_dir / f"{ing_id}{ext}"
        shutil.copy2(str(src), str(dest))
        ingredient = {
            "id": ing_id,
            "path": f"ingredients/{ing_id}{ext}",
            "label": label or src.stem,
            "addedAt": datetime.now(timezone.utc).isoformat(),
            "sourceType": source_type,
            "sourceRef": source_path,
        }
        manifest_path = proj / "ingredients.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
        else:
            manifest = {"ingredients": []}
        manifest["ingredients"].append(ingredient)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        _log(f"[ingredients] promoted {source_path} -> {ingredient['path']}")
        return {"success": True, "ingredient": ingredient}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/ingredients/remove",
    operation_id="remove_ingredient",
    summary="Delete an ingredient and its file",
)
async def remove_ingredient(
    name: str,
    body: IngredientsRemoveBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    try:
        ing_id = body.ingredientId or ""
        if not ing_id:
            raise ApiError("BAD_REQUEST", "Missing ingredientId", status_code=400)
        manifest_path = proj / "ingredients.json"
        if not manifest_path.exists():
            raise ApiError("NOT_FOUND", "No ingredients manifest", status_code=404)
        manifest = json.loads(manifest_path.read_text())
        ingredient = next(
            (i for i in manifest["ingredients"] if i["id"] == ing_id), None
        )
        if not ingredient:
            raise ApiError(
                "NOT_FOUND", f"Ingredient {ing_id} not found", status_code=404
            )
        ing_file = proj / ingredient["path"]
        if ing_file.exists():
            ing_file.unlink()
        manifest["ingredients"] = [
            i for i in manifest["ingredients"] if i["id"] != ing_id
        ]
        manifest_path.write_text(json.dumps(manifest, indent=2))
        _log(f"[ingredients] removed {ing_id}")
        return {"success": True}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


@router.post(
    "/api/projects/{name}/ingredients/update",
    operation_id="update_ingredient",
    summary="Update ingredient metadata (label)",
)
async def update_ingredient(
    name: str,
    body: IngredientsUpdateBody,
    proj: Path = Depends(project_dir_dep),
) -> dict:
    try:
        ing_id = body.ingredientId or ""
        if not ing_id:
            raise ApiError("BAD_REQUEST", "Missing ingredientId", status_code=400)
        manifest_path = proj / "ingredients.json"
        if not manifest_path.exists():
            raise ApiError("NOT_FOUND", "No ingredients manifest", status_code=404)
        manifest = json.loads(manifest_path.read_text())
        ingredient = next(
            (i for i in manifest["ingredients"] if i["id"] == ing_id), None
        )
        if not ingredient:
            raise ApiError(
                "NOT_FOUND", f"Ingredient {ing_id} not found", status_code=404
            )
        if body.label is not None:
            ingredient["label"] = body.label
        manifest_path.write_text(json.dumps(manifest, indent=2))
        return {"success": True}
    except ApiError:
        raise
    except Exception as e:
        raise ApiError("INTERNAL_ERROR", str(e), status_code=500)


__all__ = ["router"]
