# Task 21: Cloud GPU Provisioning

**Milestone**: [M7 - AI Video Stylization](../../milestones/milestone-7-ai-video-stylization.md)
**Design Reference**: None
**Estimated Time**: 4 hours
**Dependencies**: Task 20
**Status**: Not Started

---

## Objective

Automate Vast.ai GPU instance lifecycle: search for suitable instances, provision, deploy ComfyUI + models, upload frames, run render, download results, teardown.

---

## Steps

### 1. Create cloud/ Module

```
src/beatlab/render/
├── cloud.py        # Vast.ai instance management
```

### 2. Implement Instance Search & Provisioning

```python
class VastAIManager:
    def __init__(self, api_key: str | None = None): ...
    def find_instance(self, min_vram_gb: int = 16, max_price_hr: float = 2.0) -> dict: ...
    def create_instance(self, instance_id: int, image: str = "comfyui") -> str: ...
    def wait_until_ready(self, instance_id: str, timeout: int = 300) -> None: ...
    def destroy_instance(self, instance_id: str) -> None: ...
```

- Use Vast.ai REST API or `vastai` CLI
- Search for instances with sufficient VRAM (16GB+ for SDXL)
- Filter by price (default max $2/hr)
- Use pre-built ComfyUI Docker image if available

### 3. Implement Deployment

```python
def deploy_render_env(self, instance_id: str) -> str:
    """Deploy ComfyUI and models to instance. Returns ComfyUI URL."""
```

- SSH into instance
- Install ComfyUI if not in Docker image
- Download SDXL model + ControlNet model (cached if image has them)
- Start ComfyUI server
- Return accessible URL (host:port)

### 4. Implement File Transfer

```python
def upload_frames(self, instance_id: str, frames_dir: str, remote_dir: str) -> None: ...
def download_results(self, instance_id: str, remote_dir: str, local_dir: str) -> None: ...
```

- Use rsync over SSH for efficient transfer
- Compress frames for upload (tar.gz)
- Progress reporting for large transfers

### 5. Implement Full Lifecycle

```python
def render_on_cloud(
    frames_dir: str, frame_params: list[dict],
    output_dir: str, api_key: str | None = None,
) -> None:
    """Full cloud render lifecycle."""
```

1. Find and provision instance
2. Deploy ComfyUI + models
3. Upload frames + params
4. Run render via ComfyUI API
5. Download results
6. Destroy instance

### 6. Cost Estimation

```python
def estimate_cost(frame_count: int, gpu_type: str = "A100") -> dict:
    """Estimate render time and cost."""
```

- Return estimated time, cost, GPU type
- Display before starting render for user confirmation

### 7. Add Tests

- Test instance search with mock API
- Test cost estimation
- Test lifecycle with mock SSH

---

## Verification

- [ ] Vast.ai API integration works (search, create, destroy)
- [ ] ComfyUI deploys and starts on fresh instance
- [ ] Frame upload/download works via rsync
- [ ] Cost estimation is reasonable
- [ ] Instance destroyed after render completes
- [ ] Graceful handling of API errors

---

## Notes

- Requires VASTAI_API_KEY env var
- Pre-built Docker images with ComfyUI save ~10 min setup time
- Consider spot instances for cheaper renders (with resume support from Task 20)

---

**Next Task**: [Task 22: Render CLI & AI Director](task-22-render-cli.md)
