"""麦麦观鸟插件。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import asyncio
import base64
import io
import logging
import re
import time

import aiohttp
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


BAIDU_ANIMAL_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v1/animal"
BAIDU_DISH_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v2/dish"
BAIDU_PLANT_ENDPOINT = "https://aip.baidubce.com/rest/2.0/image-classify/v1/plant"
BAIDU_TOKEN_ENDPOINT = "https://aip.baidubce.com/oauth/2.0/token"
HHO_DONGNIAO_ENDPOINT = "https://ai.open.hhodata.com/api/v2/dongniao"
MAX_DOWNLOAD_IMAGE_BYTES = 4 * 1024 * 1024
HHO_MAX_IMAGE_BYTES = 2 * 1024 * 1024
TOKEN_EXPIRE_SAFETY_SECONDS = 300
HHO_SUCCESS_STATUS = "1000"
HHO_PENDING_STATUS = "1001"
BIRDWATCHING_ANNOTATED_IMAGE_SOURCE = "birdwatching_annotated_image"
ANNOTATED_IMAGE_MAX_BYTES = 2 * 1024 * 1024
ANNOTATED_IMAGE_MAX_SIDE = 1920
HHO_ANIMAL_CLASSES = "B"
HHO_DID = "maibot"
HHO_TOP_NUM = 6
HHO_POLL_ATTEMPTS = 2
HHO_POLL_INTERVAL_SECONDS = 1.0
HHO_TIMEOUT_SECONDS = 15.0
HHO_ANNOTATION_DISCLAIMER = "标注来自懂鸟api，不一定准确"

logger = logging.getLogger("plugin.maimai_birdwatching_plugin")


BIRD_KEYWORDS = (
    "鸟",
    "雀",
    "鸦",
    "鹊",
    "鸽",
    "鹦鹉",
    "鹤",
    "鹭",
    "鹰",
    "隼",
    "雕",
    "鸮",
    "猫头鹰",
    "雁",
    "鸭",
    "鹅",
    "天鹅",
    "鹬",
    "鸥",
    "鸡",
    "雉",
    "鸵鸟",
    "企鹅",
    "蜂鸟",
    "啄木鸟",
    "翠鸟",
    "燕",
    "鹃",
    "鸫",
    "鹎",
    "莺",
    "鹡鸰",
    "鹀",
    "鹟",
    "鹈鹕",
    "鹳",
    "鹱",
    "鸻",
)


def _tool_param(name: str, param_type: ToolParamType, description: str, required: bool) -> ToolParameterInfo:
    """构造工具参数声明。"""

    return ToolParameterInfo(name=name, param_type=param_type, description=description, required=required)


def _extract_nested_mapping(payload: Any) -> Dict[str, Any]:
    """从 capability 返回值中剥离常见包装层，取出业务字典。"""

    current = payload
    visited: set[int] = set()
    while isinstance(current, dict):
        current_id = id(current)
        if current_id in visited:
            break
        visited.add(current_id)

        for wrapper_key in ("result", "data"):
            nested_value = current.get(wrapper_key)
            if isinstance(nested_value, dict):
                current = nested_value
                break
        else:
            return current
    return {}


def _guess_image_format_from_name(file_name: str, default: str = "png") -> str:
    """根据文件名猜测图片格式。"""

    suffix = Path(file_name).suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg", "png", "webp", "gif", "bmp"}:
        return "jpeg" if suffix == "jpg" else suffix
    return default


def _guess_image_format_from_bytes(image_bytes: bytes, default: str = "png") -> str:
    """根据图片文件头猜测图片格式。"""

    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if image_bytes.startswith(b"GIF8"):
        return "gif"
    if image_bytes.startswith(b"BM"):
        return "bmp"
    return default


def _decode_base64_image(raw_base64: str) -> Optional[Tuple[str, str]]:
    """解析普通 Base64 或 data URL 图片内容。"""

    normalized_base64 = raw_base64.strip()
    if not normalized_base64:
        return None

    image_format = "png"
    data_url_match = re.match(
        r"^data:image/(?P<format>[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$",
        normalized_base64,
        re.DOTALL,
    )
    if data_url_match is not None:
        image_format = data_url_match.group("format").lower()
        normalized_base64 = data_url_match.group("data").strip()

    try:
        image_bytes = base64.b64decode(normalized_base64, validate=True)
    except Exception:
        return None
    if not image_bytes:
        return None

    return _guess_image_format_from_bytes(image_bytes, image_format), base64.b64encode(image_bytes).decode("utf-8")


def _read_image_file(image_path: Path) -> Optional[Tuple[str, str]]:
    """读取本地图片文件并返回格式与 Base64。"""

    if not image_path.exists() or not image_path.is_file():
        return None
    image_bytes = image_path.read_bytes()
    if not image_bytes or len(image_bytes) > MAX_DOWNLOAD_IMAGE_BYTES:
        return None
    image_format = _guess_image_format_from_bytes(image_bytes, _guess_image_format_from_name(image_path.name))
    return image_format, base64.b64encode(image_bytes).decode("utf-8")


def _resolve_file_url(file_url: str) -> Optional[Path]:
    """将 file:// URL 转换为本地路径。"""

    parsed_url = urlparse(file_url)
    if parsed_url.scheme.lower() != "file":
        return None
    if parsed_url.netloc and parsed_url.path:
        return Path(f"//{parsed_url.netloc}{unquote(parsed_url.path)}")
    return Path(unquote(parsed_url.path))


def _download_image_url(image_url: str) -> Optional[Tuple[str, str]]:
    """下载图片 URL 并返回格式与 Base64。"""

    request = Request(image_url, headers={"User-Agent": "MaiBot-birdwatching-plugin/1.0"})
    with urlopen(request, timeout=10) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith("image/"):
            return None

        image_bytes = response.read(MAX_DOWNLOAD_IMAGE_BYTES + 1)
    if not image_bytes or len(image_bytes) > MAX_DOWNLOAD_IMAGE_BYTES:
        return None

    url_path = unquote(urlparse(image_url).path)
    guessed_format = _guess_image_format_from_name(url_path)
    if content_type.startswith("image/"):
        guessed_format = content_type.split(";", 1)[0].split("/", 1)[1].strip() or guessed_format
    image_format = _guess_image_format_from_bytes(image_bytes, guessed_format)
    return image_format, base64.b64encode(image_bytes).decode("utf-8")


def _is_windows_drive_path(reference: str) -> bool:
    """判断字符串是否像 Windows 盘符路径。"""

    return bool(re.match(r"^[A-Za-z]:[\\/]", reference.strip()))


def _read_image_reference(reference: str) -> Optional[Tuple[str, str]]:
    """从本地路径、file URL、http(s) URL、data URL 或 Base64 引用中读取图片。"""

    normalized_reference = reference.strip()
    if not normalized_reference:
        return None

    decoded_base64 = _decode_base64_image(normalized_reference)
    if decoded_base64 is not None:
        return decoded_base64

    parsed_url = urlparse(normalized_reference)
    normalized_scheme = parsed_url.scheme.lower()
    if not _is_windows_drive_path(normalized_reference):
        if normalized_scheme == "file":
            file_path = _resolve_file_url(normalized_reference)
            return _read_image_file(file_path) if file_path is not None else None
        if normalized_scheme in {"http", "https"}:
            return _download_image_url(normalized_reference)
        if normalized_scheme:
            return None

    try:
        image_path = Path(normalized_reference)
    except OSError:
        return None
    return _read_image_file(image_path)


def _extract_image_hash(component: Dict[str, Any]) -> str:
    """从图片消息段中提取图片 hash。"""

    for key in ("hash", "binary_hash", "image_hash", "file_hash"):
        value = str(component.get(key) or "").strip()
        if value:
            return value
    return ""


def _read_cached_image_by_hash(image_hash: str) -> Optional[Tuple[str, str]]:
    """通过图片 hash 从本地图片缓存数据库读取图片。"""

    if not image_hash:
        return None

    try:
        from sqlmodel import select

        from src.common.database.database import get_db_session
        from src.common.database.database_model import Images, ImageType

        with get_db_session() as db:
            statement = select(Images).filter_by(image_hash=image_hash, image_type=ImageType.IMAGE).limit(1)
            image_record = db.exec(statement).first()
            if image_record is None or image_record.no_file_flag:
                return None
            raw_full_path = str(image_record.full_path or "").strip()

        if not raw_full_path:
            return None
        return _read_image_file(Path(raw_full_path).expanduser().resolve())
    except Exception as exc:
        logger.info("读取图片缓存失败：hash=%s error=%s", image_hash[:12], exc, exc_info=True)
        return None


def _iter_image_reference_values(value: Any) -> List[str]:
    """从图片消息段 data 或附加字段中收集可能的图片引用。"""

    if isinstance(value, dict):
        references: List[str] = []
        for key in ("binary_data_base64", "base64", "data_url", "url", "file", "file_path", "path", "data"):
            references.extend(_iter_image_reference_values(value.get(key)))
        return references

    if isinstance(value, list):
        references = []
        for item in value:
            references.extend(_iter_image_reference_values(item))
        return references

    normalized_value = str(value or "").strip()
    if not normalized_value:
        return []
    if normalized_value.startswith("[") and normalized_value.endswith("]") and not normalized_value.startswith("[CQ:"):
        return []

    references = [normalized_value]
    cq_url_match = re.search(r"(?:url|file|path)=([^,\]]+)", normalized_value)
    if cq_url_match is not None:
        references.append(cq_url_match.group(1).strip())
    return references


def _is_bird_name(name: str) -> bool:
    """粗略判断识别结果名称是否为鸟类。"""

    normalized_name = str(name or "").strip()
    return bool(normalized_name) and any(keyword in normalized_name for keyword in BIRD_KEYWORDS)


def _score_to_text(score: Any) -> str:
    """格式化识别接口返回的置信分数。"""

    try:
        numeric_score = float(score)
    except (TypeError, ValueError):
        return ""
    if numeric_score <= 1:
        return f"{numeric_score * 100:.1f}%"
    return f"{numeric_score:.3g}"


def _status_text(payload: Dict[str, Any]) -> str:
    """读取接口返回状态码。"""

    return str(payload.get("status") or "").strip()


def _is_hho_success(payload: Dict[str, Any]) -> bool:
    """判断 HHo 响应是否为成功状态。"""

    status = _status_text(payload).lower()
    return status in {HHO_SUCCESS_STATUS, "success"}


def _normalize_hho_payload(payload: Any) -> Dict[str, Any]:
    """兼容 HHo 文档示例和实际数组式 JSON 返回。"""

    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        status = str(payload[0]).strip() if payload else ""
        message = str(payload[1]).strip() if len(payload) >= 3 else ""
        data = payload[2] if len(payload) >= 3 else payload[1] if len(payload) >= 2 else []
        return {"status": status, "message": message, "data": data, "raw_response": payload}
    return {}


def _message_text(payload: Dict[str, Any], default: str = "未知错误") -> str:
    """读取接口返回错误描述。"""

    return str(payload.get("message") or payload.get("error_msg") or payload.get("error_description") or default).strip()


def _split_hho_name(raw_name: Any) -> Tuple[str, str, str]:
    """拆分 HHo 返回的“中文名|英文名|拉丁名”。"""

    name_parts = [part.strip() for part in str(raw_name or "").split("|")]
    chinese_name = name_parts[0] if name_parts else ""
    english_name = name_parts[1] if len(name_parts) > 1 else ""
    scientific_name = name_parts[2] if len(name_parts) > 2 else ""
    return chinese_name, english_name, scientific_name


def _extract_hho_recognition_id(payload: Dict[str, Any]) -> str:
    """从 HHo 上传图片响应中提取识别 ID。"""

    raw_data = payload.get("data")
    if isinstance(raw_data, dict):
        recognition_id = str(raw_data.get("recognitionId") or raw_data.get("resultid") or "").strip()
        if recognition_id:
            return recognition_id
    if isinstance(raw_data, str):
        return raw_data.strip()
    if isinstance(raw_data, list):
        if len(raw_data) >= 2 and str(raw_data[0]).strip() == HHO_SUCCESS_STATUS:
            return str(raw_data[1] or "").strip()
        for item in raw_data:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                recognition_id = str(item.get("recognitionId") or item.get("resultid") or "").strip()
                if recognition_id:
                    return recognition_id
    return ""


def _extract_hho_baike_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """从 HHo 百科响应中提取百科字典。"""

    raw_data = payload.get("data")
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, list):
        if len(raw_data) >= 2 and str(raw_data[0]).strip() == HHO_SUCCESS_STATUS and isinstance(raw_data[1], dict):
            return raw_data[1]
        for item in raw_data:
            if isinstance(item, dict):
                return item
    return {}


def _is_success_result(result: Any) -> bool:
    """兼容常见 capability 成功返回格式。"""

    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        return bool(result.get("success"))
    return False


def _load_annotation_font(font_size: int) -> Any:
    """加载支持中文的标注字体。"""

    from PIL import ImageFont

    font_paths = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    )
    for font_path in font_paths:
        try:
            if Path(font_path).exists():
                return ImageFont.truetype(font_path, font_size)
        except Exception:
            continue
    return ImageFont.load_default()


def _annotation_label(item: Dict[str, Any]) -> str:
    """构造图片标注文字。"""

    return str(item.get("name") or "").strip() or "鸟"


def _draw_annotation_disclaimer(draw: Any, width: int, height: int, base_font_size: int) -> None:
    """在标注图左下角绘制小号来源声明。"""

    font_size = max(8, min(14, base_font_size // 2))
    margin = max(6, font_size // 2)
    available_text_width = width - margin * 2
    if available_text_width <= 0:
        return

    while True:
        font = _load_annotation_font(font_size)
        text_bbox = draw.textbbox((0, 0), HHO_ANNOTATION_DISCLAIMER, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        if text_width <= available_text_width or font_size <= 8:
            break
        font_size -= 1
        margin = max(6, font_size // 2)
        available_text_width = width - margin * 2
        if available_text_width <= 0:
            return

    if text_width > available_text_width:
        return

    draw.text(
        (margin, max(0, height - margin - text_height - text_bbox[1])),
        HHO_ANNOTATION_DISCLAIMER,
        fill=(230, 230, 230, 210),
        font=font,
    )


def _unique_annotation_targets(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """每个目标框只取置信度最高的一个候选用于画框。"""

    targets: List[Dict[str, Any]] = []
    seen_keys: set[str] = set()
    for index, item in enumerate(results):
        box = item.get("box")
        if not isinstance(box, list) or len(box) != 4:
            continue
        target_index = item.get("target_index")
        key = str(target_index) if target_index is not None else ",".join(str(part) for part in box)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        targets.append({**item, "_annotation_index": index + 1})
    return targets


def _save_annotated_jpeg(image: Any) -> bytes:
    """把标注图保存成适合聊天发送的 JPEG。"""

    qualities = (88, 82, 76, 70, 64)
    for quality in qualities:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        image_bytes = output.getvalue()
        if len(image_bytes) <= ANNOTATED_IMAGE_MAX_BYTES or quality == qualities[-1]:
            return image_bytes
    return image_bytes


def _save_hho_upload_jpeg(image: Any) -> bytes:
    """把 PNG 转成 HHo 可上传的 JPEG，并尽量压到接口限制内。"""

    qualities = (92, 88, 84, 80, 76, 72, 68, 64, 60)
    for quality in qualities:
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
        image_bytes = output.getvalue()
        if len(image_bytes) <= HHO_MAX_IMAGE_BYTES or quality == qualities[-1]:
            return image_bytes
    return image_bytes


def _convert_png_to_hho_jpeg(image_bytes: bytes) -> bytes:
    """将 PNG 图片转换为 HHo 懂鸟接口接受的 JPEG。"""

    try:
        from PIL import Image
    except Exception as exc:
        raise RuntimeError("PNG 自动转 JPG 需要 Pillow，但当前环境不可用。") from exc

    try:
        with Image.open(io.BytesIO(image_bytes)) as image:
            rgba_image = image.convert("RGBA")
            rgb_image = Image.new("RGB", rgba_image.size, (255, 255, 255))
            rgb_image.paste(rgba_image, mask=rgba_image.getchannel("A"))
            return _save_hho_upload_jpeg(rgb_image)
    except Exception as exc:
        raise RuntimeError("PNG 图片转换为 JPG 失败，无法提交给 HHo 懂鸟接口。") from exc


def _prepare_hho_upload_image(image_bytes: bytes, image_format: str) -> Tuple[bytes, str]:
    """准备 HHo 上传图片；HHo 仅收 JPEG，因此 PNG 会自动转 JPG。"""

    normalized_format = image_format.strip().lower()
    if normalized_format == "jpg":
        normalized_format = "jpeg"
    if normalized_format == "png":
        image_bytes = _convert_png_to_hho_jpeg(image_bytes)
        normalized_format = "jpeg"
    elif normalized_format != "jpeg":
        raise RuntimeError("HHo 懂鸟接口仅支持 jpg/jpeg/png 图片；png 会自动转 jpg。")

    if not image_bytes or len(image_bytes) > HHO_MAX_IMAGE_BYTES:
        raise RuntimeError("HHo 懂鸟接口要求上传图片不超过 2MB；png 转 jpg 后也需要低于该限制。")
    return image_bytes, normalized_format


def _resize_annotated_image_if_needed(image: Any) -> Any:
    """必要时缩小标注图，避免高分辨率照片发送体积过大。"""

    width, height = image.size
    longest_side = max(width, height)
    if longest_side <= ANNOTATED_IMAGE_MAX_SIDE:
        return image

    try:
        from PIL import Image
    except Exception:
        return image

    scale = ANNOTATED_IMAGE_MAX_SIDE / longest_side
    resized_size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(resized_size, Image.Resampling.LANCZOS)


def _get_image_dimensions_from_bytes(image_bytes: bytes) -> Optional[Tuple[int, int]]:
    """读取图片尺寸；失败时返回 None，不影响识别流程。"""

    try:
        from PIL import Image

        with Image.open(io.BytesIO(image_bytes)) as image:
            return image.size
    except Exception as exc:
        logger.info("读取 HHo 图片尺寸失败，小框比例过滤将退化为像素过滤：%s", exc)
        return None


def _hho_box_size(box: Any, image_size: Optional[Tuple[int, int]] = None) -> Optional[Tuple[float, float]]:
    """计算 HHo 目标框宽高。"""

    if not isinstance(box, list) or len(box) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(part) for part in box]
    except (TypeError, ValueError):
        return None

    if image_size is not None:
        image_width, image_height = image_size
        if image_width <= 0 or image_height <= 0:
            return None
        x1 = max(0.0, min(float(image_width), x1))
        x2 = max(0.0, min(float(image_width), x2))
        y1 = max(0.0, min(float(image_height), y1))
        y2 = max(0.0, min(float(image_height), y2))

    return abs(x2 - x1), abs(y2 - y1)


def _build_annotated_image_base64(image_base64: str, results: List[Dict[str, Any]]) -> str:
    """生成带 HHo 目标框和鸟名标注的 JPEG Base64。"""

    targets = _unique_annotation_targets(results)
    if not targets:
        return ""

    try:
        from PIL import Image, ImageDraw
    except Exception as exc:
        logger.info("Pillow 不可用，跳过鸟类标注图生成：%s", exc)
        return ""

    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
        image = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    except Exception as exc:
        logger.info("鸟类标注图读取原图失败：%s", exc, exc_info=True)
        return ""

    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    line_width = max(3, min(width, height) // 150)
    font_size = max(18, min(42, min(width, height) // 22))
    font = _load_annotation_font(font_size)
    padding_x = max(8, font_size // 3)
    padding_y = max(5, font_size // 5)
    palette = (
        (255, 204, 64, 255),
        (68, 207, 255, 255),
        (100, 230, 140, 255),
        (255, 128, 160, 255),
        (190, 150, 255, 255),
    )

    for index, item in enumerate(targets):
        try:
            x1, y1, x2, y2 = [int(round(float(part))) for part in item["box"]]
        except (TypeError, ValueError):
            continue

        x1, x2 = sorted((max(0, min(width - 1, x1)), max(0, min(width - 1, x2))))
        y1, y2 = sorted((max(0, min(height - 1, y1)), max(0, min(height - 1, y2))))
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue

        color = palette[index % len(palette)]
        draw.rectangle((x1, y1, x2, y2), outline=color, width=line_width)
        label = _annotation_label(item)
        text_bbox = draw.textbbox((0, 0), label, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        label_width = text_width + padding_x * 2
        label_height = text_height + padding_y * 2
        label_x1 = max(0, min(x1, width - label_width))
        label_y1 = y1 - label_height - line_width
        if label_y1 < 0:
            label_y1 = min(height - label_height, y1 + line_width)
        label_x2 = label_x1 + label_width
        label_y2 = label_y1 + label_height
        draw.rounded_rectangle(
            (label_x1, label_y1, label_x2, label_y2),
            radius=max(4, font_size // 4),
            fill=color,
        )
        draw.text(
            (label_x1 + padding_x, label_y1 + padding_y - text_bbox[1]),
            label,
            fill=(20, 22, 24, 255),
            font=font,
        )

    _draw_annotation_disclaimer(draw, width, height, font_size)
    annotated = Image.alpha_composite(image, overlay).convert("RGB")
    annotated = _resize_annotated_image_if_needed(annotated)
    annotated_bytes = _save_annotated_jpeg(annotated)
    return base64.b64encode(annotated_bytes).decode("utf-8")


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class BaiduImageRecognitionConfig(PluginConfigBase):
    """百度智能云图像识别配置。"""

    __ui_label__ = "百度识别"
    __ui_icon__ = "bird"
    __ui_order__ = 1

    api_key: str = Field(default="", description="百度智能云应用的 API Key")
    secret_key: str = Field(default="", description="百度智能云应用的 Secret Key")
    top_num: int = Field(default=6, ge=1, le=10, description="返回预测得分 Top 结果数")
    baike_num: int = Field(default=1, ge=0, le=5, description="返回百科信息条数，0 表示不请求百科")
    dish_filter_threshold: float = Field(default=0.95, ge=0.0, le=1.0, description="菜品识别过滤阈值，越高越严格")
    timeout_seconds: float = Field(default=15.0, ge=3.0, le=60.0, description="百度接口请求超时时间")


class BirdRecognitionConfig(PluginConfigBase):
    """鸟类识别服务选择。"""

    __ui_label__ = "鸟类识别"
    __ui_icon__ = "bird"
    __ui_order__ = 2

    provider: str = Field(default="baidu", description="鸟类识别服务：baidu 或 hho")


class HHoDongniaoConfig(PluginConfigBase):
    """HHo 懂鸟动物识别配置。"""

    __ui_label__ = "HHo 懂鸟"
    __ui_icon__ = "feather"
    __ui_order__ = 3

    api_key: str = Field(default="", description="HHo AI 开放平台 api_key")
    area_code: str = Field(default="", description="可选地区码，留空表示不按地区过滤")
    baike_num: int = Field(default=0, ge=0, le=5, description="HHo 为前几个候选补充百科信息，0 表示不请求百科")
    send_annotated_image: bool = Field(default=True, description="识别成功后是否额外发送带框标注图")
    min_box_area_ratio: float = Field(default=0.0005, ge=0.0, le=1.0, description="HHo 小目标框面积过滤比例，0 表示关闭")


class ToolSwitchConfig(PluginConfigBase):
    """工具启用开关。"""

    __ui_label__ = "工具开关"
    __ui_icon__ = "toggle-left"
    __ui_order__ = 4

    recognize_bird: bool = Field(default=True, description="是否启用鸟类识别工具")
    recognize_animal: bool = Field(default=True, description="是否启用动物识别工具")
    recognize_plant: bool = Field(default=True, description="是否启用植物识别工具")
    recognize_dish: bool = Field(default=False, description="是否启用菜品识别工具")


class BirdwatchingPluginConfig(PluginConfigBase):
    """麦麦观鸟插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    bird: BirdRecognitionConfig = Field(default_factory=BirdRecognitionConfig)
    baidu: BaiduImageRecognitionConfig = Field(default_factory=BaiduImageRecognitionConfig)
    hho: HHoDongniaoConfig = Field(default_factory=HHoDongniaoConfig)
    tools: ToolSwitchConfig = Field(default_factory=ToolSwitchConfig)


class BirdwatchingPlugin(MaiBotPlugin):
    """麦麦观鸟插件。"""

    config_model = BirdwatchingPluginConfig

    def __init__(self) -> None:
        super().__init__()
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def on_load(self) -> None:
        """插件加载回调。"""

    async def on_unload(self) -> None:
        """插件卸载回调。"""

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """插件配置更新回调。"""

        del scope
        del config_data
        del version
        self._access_token = ""
        self._access_token_expires_at = 0.0

    async def _find_message_by_id(self, stream_id: str, msg_id: str) -> Optional[Dict[str, Any]]:
        """通过 Host 提供的单条消息查询能力按消息 ID 查找目标消息。"""

        normalized_stream_id = stream_id.strip()
        normalized_msg_id = msg_id.strip()
        if not normalized_stream_id or not normalized_msg_id:
            return None

        lookup_result = await self.ctx.call_capability(
            "message.get_by_id",
            message_id=normalized_msg_id,
            chat_id=normalized_stream_id,
        )
        if lookup_result is None:
            return None
        if not isinstance(lookup_result, dict):
            raise RuntimeError("message.get_by_id 返回格式异常。")
        if "message_id" in lookup_result and "raw_message" in lookup_result:
            return lookup_result

        capability_result = _extract_nested_mapping(lookup_result)
        if lookup_result.get("success") is False or capability_result.get("success") is False:
            raise RuntimeError(
                str(capability_result.get("error") or lookup_result.get("error") or "message.get_by_id 查询失败。")
            )
        if "message_id" in capability_result and "raw_message" in capability_result:
            return capability_result

        direct_message = capability_result.get("message")
        if direct_message is None:
            return None
        if not isinstance(direct_message, dict):
            raise RuntimeError("message.get_by_id 返回的 message 字段格式异常。")
        return direct_message

    @staticmethod
    def _extract_image_from_message(message: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """从消息中提取第一张图片。"""

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return None, None, "目标消息结构不合法，无法读取图片。"

        for component in raw_message:
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "").strip().lower() != "image":
                continue

            binary_data_base64 = str(component.get("binary_data_base64") or "").strip()
            if binary_data_base64:
                image_result = _decode_base64_image(binary_data_base64)
                if image_result is not None:
                    image_format, image_base64 = image_result
                    return image_format, image_base64, None

            image_result = _read_cached_image_by_hash(_extract_image_hash(component))
            if image_result is not None:
                image_format, image_base64 = image_result
                return image_format, image_base64, None

            references = []
            references.extend(_iter_image_reference_values(component.get("data")))
            references.extend(_iter_image_reference_values(component.get("url")))
            references.extend(_iter_image_reference_values(component.get("file")))
            references.extend(_iter_image_reference_values(component.get("file_path")))
            references.extend(_iter_image_reference_values(component.get("path")))
            references.extend(_iter_image_reference_values(component.get("data_url")))
            for reference in references:
                image_result = _read_image_reference(reference)
                if image_result is not None:
                    image_format, image_base64 = image_result
                    return image_format, image_base64, None

            return None, None, "目标消息里有图片标记，但拿不到可供识别的图片二进制内容。"

        return None, None, "目标消息中没有图片。"

    async def _load_message_image(self, msg_id: str, stream_id: str) -> Tuple[str, str]:
        """按消息 ID 提取图片格式和 Base64 内容。"""

        normalized_msg_id = msg_id.strip()
        normalized_stream_id = stream_id.strip()
        if not normalized_msg_id:
            raise RuntimeError("缺少 msg_id，无法识别图片。")
        if not normalized_stream_id:
            raise RuntimeError("缺少当前会话 stream_id，无法按消息 ID 查询图片。")

        target_message = await self._find_message_by_id(normalized_stream_id, normalized_msg_id)
        if target_message is None:
            raise RuntimeError(f"未找到消息 ID 为 {normalized_msg_id} 的消息。")

        image_format, image_base64, image_error = self._extract_image_from_message(target_message)
        if image_error is not None or not image_format or not image_base64:
            raise RuntimeError(image_error or "无法读取目标消息中的图片。")
        return image_format, image_base64

    def _get_bird_provider(self) -> str:
        """读取并校验鸟类识别服务。"""

        provider = self.config.bird.provider.strip().lower()
        if provider not in {"hho", "baidu"}:
            raise RuntimeError("鸟类识别服务 provider 只能配置为 hho 或 baidu。")
        return provider

    def _require_baidu_config(self) -> Tuple[str, str]:
        """读取并校验百度鉴权配置。"""

        api_key = self.config.baidu.api_key.strip()
        secret_key = self.config.baidu.secret_key.strip()
        if not api_key or not secret_key:
            raise RuntimeError("尚未配置百度智能云 API Key 或 Secret Key。")
        return api_key, secret_key

    def _require_hho_config(self) -> str:
        """读取并校验 HHo 鉴权配置。"""

        api_key = self.config.hho.api_key.strip()
        if not api_key:
            raise RuntimeError("尚未配置 HHo AI 开放平台 api_key。")
        return api_key

    async def _post_hho_form(
        self,
        session: aiohttp.ClientSession,
        form: aiohttp.FormData,
        service_name: str,
    ) -> Dict[str, Any]:
        """提交 HHo multipart/form-data 请求并解析 JSON。"""

        headers = {"api_key": self._require_hho_config()}
        async with session.post(HHO_DONGNIAO_ENDPOINT, data=form, headers=headers) as response:
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                response_text = await response.text()
                raise RuntimeError(f"HHo {service_name}返回不是 JSON：HTTP {response.status} {response_text[:200]}") from exc
            normalized_payload = _normalize_hho_payload(payload)
            if response.status >= 400:
                raise RuntimeError(f"HHo {service_name}请求失败：HTTP {response.status} {payload}")

        if not normalized_payload:
            raise RuntimeError(f"HHo {service_name}返回格式异常。")
        return normalized_payload

    async def _send_annotated_bird_image(
        self,
        stream_id: str,
        image_base64: str,
        results: List[Dict[str, Any]],
    ) -> bool:
        """额外发送带框鸟类标注图；失败不影响识别工具结果。"""

        if not self.config.hho.send_annotated_image:
            return False
        normalized_stream_id = stream_id.strip()
        if not normalized_stream_id:
            return False

        annotated_image_base64 = _build_annotated_image_base64(image_base64, results)
        if not annotated_image_base64:
            return False

        call_capability = getattr(self.ctx, "call_capability", None)
        if not callable(call_capability):
            return False
        try:
            result = await call_capability(
                "send.image",
                image_base64=annotated_image_base64,
                stream_id=normalized_stream_id,
                sync_to_maisaka_history=True,
                maisaka_source_kind=BIRDWATCHING_ANNOTATED_IMAGE_SOURCE,
            )
        except Exception as exc:
            logger.info("发送鸟类标注图失败：%s", exc, exc_info=True)
            return False
        if not _is_success_result(result):
            logger.info("发送鸟类标注图失败：result=%s", result)
            return False
        return True

    async def _get_access_token(self) -> str:
        """获取并缓存百度 access_token。"""

        now = time.time()
        if self._access_token and now < self._access_token_expires_at:
            return self._access_token

        api_key, secret_key = self._require_baidu_config()
        timeout = aiohttp.ClientTimeout(total=self.config.baidu.timeout_seconds)
        params = {
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret_key,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(BAIDU_TOKEN_ENDPOINT, params=params, headers=headers, data="") as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"百度鉴权请求失败：HTTP {response.status} {payload}")

        if not isinstance(payload, dict):
            raise RuntimeError("百度鉴权返回格式异常。")
        token = str(payload.get("access_token") or "").strip()
        if not token:
            error = payload.get("error_description") or payload.get("error") or "未返回 access_token"
            raise RuntimeError(f"百度鉴权失败：{error}")

        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        self._access_token = token
        self._access_token_expires_at = now + max(60, expires_in - TOKEN_EXPIRE_SAFETY_SECONDS)
        return token

    async def _recognize_baidu_image(
        self,
        image_base64: str,
        endpoint: str,
        service_name: str,
        *,
        include_top_num: bool = False,
        include_dish_filter_threshold: bool = False,
    ) -> Dict[str, Any]:
        """调用百度图像识别接口。"""

        if len(image_base64.encode("utf-8")) > MAX_DOWNLOAD_IMAGE_BYTES:
            raise RuntimeError(f"图片 Base64 编码后超过 4MB，无法提交给百度{service_name}接口。")

        access_token = await self._get_access_token()
        timeout = aiohttp.ClientTimeout(total=self.config.baidu.timeout_seconds)
        params = {"access_token": access_token}
        data: Dict[str, Any] = {
            "image": image_base64,
        }
        if include_top_num:
            data["top_num"] = self.config.baidu.top_num
        if include_dish_filter_threshold:
            data["filter_threshold"] = self.config.baidu.dish_filter_threshold
        if self.config.baidu.baike_num > 0:
            data["baike_num"] = self.config.baidu.baike_num

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                params=params,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                payload = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(f"百度{service_name}请求失败：HTTP {response.status} {payload}")

        if not isinstance(payload, dict):
            raise RuntimeError(f"百度{service_name}返回格式异常。")
        if payload.get("error_code") is not None:
            error_msg = payload.get("error_msg") or payload.get("error_description") or "未知错误"
            raise RuntimeError(f"百度{service_name}失败：{payload.get('error_code')} {error_msg}")
        return payload

    def _build_hho_upload_form(self, image_bytes: bytes, image_format: str) -> aiohttp.FormData:
        """构造 HHo 图片上传表单。"""

        upload_bytes, _ = _prepare_hho_upload_image(image_bytes, image_format)

        form = aiohttp.FormData()
        form.add_field("image", upload_bytes, filename="bird.jpg", content_type="image/jpeg")
        form.add_field("upload", "1")
        form.add_field("class", HHO_ANIMAL_CLASSES)
        form.add_field("did", HHO_DID)
        area_code = self.config.hho.area_code.strip()
        if area_code:
            form.add_field("area", area_code)
        return form

    async def _upload_hho_image(
        self,
        session: aiohttp.ClientSession,
        image_bytes: bytes,
        image_format: str,
    ) -> Tuple[Dict[str, Any], str]:
        """上传图片到 HHo 并返回识别 ID。"""

        payload = await self._post_hho_form(session, self._build_hho_upload_form(image_bytes, image_format), "上传图片")
        status = _status_text(payload)
        if not _is_hho_success(payload):
            raise RuntimeError(f"HHo 上传图片失败：{status or '无状态码'} {_message_text(payload)}")

        recognition_id = _extract_hho_recognition_id(payload)
        if not recognition_id:
            raise RuntimeError("HHo 上传图片成功但未返回 recognitionId。")
        return payload, recognition_id

    async def _fetch_hho_result(self, session: aiohttp.ClientSession, recognition_id: str) -> Dict[str, Any]:
        """按识别 ID 获取 HHo 识别结果。"""

        form = aiohttp.FormData()
        form.add_field("resultid", recognition_id, content_type="text/plain")
        return await self._post_hho_form(session, form, "获取识别结果")

    async def _poll_hho_result(self, session: aiohttp.ClientSession, recognition_id: str) -> Dict[str, Any]:
        """轮询 HHo 识别结果。"""

        last_payload: Dict[str, Any] = {}
        for _ in range(HHO_POLL_ATTEMPTS):
            await asyncio.sleep(HHO_POLL_INTERVAL_SECONDS)
            payload = await self._fetch_hho_result(session, recognition_id)
            last_payload = payload
            status = _status_text(payload)
            if _is_hho_success(payload):
                return payload
            if status == HHO_PENDING_STATUS:
                continue
            raise RuntimeError(f"HHo 获取识别结果失败：{status or '无状态码'} {_message_text(payload)}")

        status = _status_text(last_payload)
        message = _message_text(last_payload, "结果未生成")
        raise RuntimeError(f"HHo 识别结果超时：{status or HHO_PENDING_STATUS} {message}")

    async def _fetch_hho_baike(
        self,
        session: aiohttp.ClientSession,
        animal_id: Any,
        animal_class: str,
    ) -> Dict[str, Any]:
        """按 HHo 动物 ID 获取百科资料。"""

        form = aiohttp.FormData()
        form.add_field("animalid", str(animal_id), content_type="text/plain")
        form.add_field("class", animal_class, content_type="text/plain")
        payload = await self._post_hho_form(session, form, "获取百科资料")
        status = _status_text(payload)
        if not _is_hho_success(payload):
            logger.info("HHo 百科资料请求失败：animal_id=%s class=%s payload=%s", animal_id, animal_class, payload)
            return {}
        return _extract_hho_baike_data(payload)

    @staticmethod
    def _merge_hho_baike(result: Dict[str, Any], baike_data: Dict[str, Any]) -> None:
        """把 HHo 百科资料合并到规范化结果中。"""

        if not baike_data:
            return

        descriptions = baike_data.get("描述")
        if isinstance(descriptions, dict):
            result["description"] = str(
                descriptions.get("综述")
                or descriptions.get("外形特征")
                or descriptions.get("地理分布")
                or descriptions.get("生活习性")
                or ""
            ).strip()

        baike_url = str(baike_data.get("英文维基网址") or "").strip()
        if baike_url:
            result["baike_url"] = baike_url
        result["baike"] = baike_data

    def _is_hho_box_large_enough(self, result: Dict[str, Any], image_size: Optional[Tuple[int, int]]) -> bool:
        """判断 HHo 目标框是否足够可信。"""

        box_size = _hho_box_size(result.get("box"), image_size)
        if box_size is None:
            return True

        box_width, box_height = box_size
        if box_width <= 0 or box_height <= 0:
            return False

        if image_size is None:
            return True

        image_width, image_height = image_size
        image_area = image_width * image_height
        if image_area <= 0:
            return True

        min_area_ratio = self.config.hho.min_box_area_ratio
        if min_area_ratio > 0 and (box_width * box_height) / image_area < min_area_ratio:
            return False

        return True

    def _filter_hho_results_by_box_size(
        self,
        results: List[Dict[str, Any]],
        image_size: Optional[Tuple[int, int]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """过滤 HHo 过小目标框，降低远处小点导致的误识别。"""

        kept_results: List[Dict[str, Any]] = []
        filtered_results: List[Dict[str, Any]] = []
        for result in results:
            if self._is_hho_box_large_enough(result, image_size):
                kept_results.append(result)
            else:
                filtered_results.append(result)

        if filtered_results:
            logger.info(
                "HHo 小框过滤：filtered=%s kept=%s image_size=%s dropped=%s",
                len(filtered_results),
                len(kept_results),
                image_size,
                [
                    {
                        "target_index": item.get("target_index"),
                        "name": item.get("name"),
                        "score": item.get("score"),
                        "box": item.get("box"),
                    }
                    for item in filtered_results[:8]
                ],
            )
        return kept_results, filtered_results

    @staticmethod
    def _serialize_hho_results(payload: Dict[str, Any], top_num: int) -> List[Dict[str, Any]]:
        """规范化 HHo 懂鸟识别结果。"""

        raw_targets = payload.get("data")
        if (
            isinstance(raw_targets, list)
            and len(raw_targets) >= 2
            and str(raw_targets[0]).strip() == HHO_SUCCESS_STATUS
            and isinstance(raw_targets[1], list)
        ):
            raw_targets = raw_targets[1]
        if not isinstance(raw_targets, list):
            return []

        results: List[Dict[str, Any]] = []
        for target_index, target in enumerate(raw_targets, start=1):
            if not isinstance(target, dict):
                continue
            box = target.get("box")
            raw_candidates = target.get("list")
            if not isinstance(raw_candidates, list):
                continue

            for candidate in raw_candidates[:top_num]:
                if not isinstance(candidate, list) or len(candidate) < 4:
                    continue
                chinese_name, english_name, scientific_name = _split_hho_name(candidate[1])
                if not chinese_name:
                    continue
                results.append(
                    {
                        "name": chinese_name,
                        "score": candidate[0],
                        "english_name": english_name,
                        "scientific_name": scientific_name,
                        "animal_id": candidate[2],
                        "animal_class": str(candidate[3] or "").strip(),
                        "box": box,
                        "target_index": target_index,
                    }
                )
        return results

    async def _recognize_hho_bird_image(
        self,
        image_format: str,
        image_base64: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """调用 HHo 懂鸟接口识别鸟类。"""

        try:
            image_bytes = base64.b64decode(image_base64, validate=True)
        except Exception as exc:
            raise RuntimeError("图片 Base64 内容异常，无法提交给 HHo 懂鸟接口。") from exc

        timeout = aiohttp.ClientTimeout(total=HHO_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            upload_payload, recognition_id = await self._upload_hho_image(session, image_bytes, image_format)
            result_payload = await self._poll_hho_result(session, recognition_id)
            raw_results = self._serialize_hho_results(result_payload, HHO_TOP_NUM)
            image_size = _get_image_dimensions_from_bytes(image_bytes)
            results, filtered_results = self._filter_hho_results_by_box_size(raw_results, image_size)

            for item in results[: self.config.hho.baike_num]:
                animal_id = item.get("animal_id")
                animal_class = str(item.get("animal_class") or "").strip()
                if animal_id is None or not animal_class:
                    continue
                baike_data = await self._fetch_hho_baike(session, animal_id, animal_class)
                self._merge_hho_baike(item, baike_data)

        payload = {
            "recognition_id": recognition_id,
            "upload_response": upload_payload,
            "result_response": result_payload,
            "image_size": list(image_size) if image_size is not None else None,
            "raw_result_count": len(raw_results),
            "filtered_result_count": len(filtered_results),
            "filtered_results": filtered_results,
        }
        return payload, results

    @staticmethod
    def _serialize_results(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """规范化百度图像识别结果。"""

        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            return []

        results: List[Dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            baike_info = item.get("baike_info")
            if not isinstance(baike_info, dict):
                baike_info = {}
            result = {
                "name": str(item.get("name") or "").strip(),
                "score": item.get("score", item.get("probability")),
                "calorie": item.get("calorie"),
                "baike_url": str(baike_info.get("baike_url") or "").strip(),
                "description": str(baike_info.get("description") or "").strip(),
            }
            if result["name"]:
                results.append(result)
        return results

    @staticmethod
    def _format_results(results: List[Dict[str, Any]], title: str) -> str:
        """格式化识别结果。"""

        if not results:
            return "识别接口没有返回可用的识别结果。"

        lines = [title]
        for index, item in enumerate(results, start=1):
            score_text = _score_to_text(item.get("score"))
            suffix = f"（置信度 {score_text}）" if score_text else ""
            target_index = item.get("target_index")
            target_prefix = f"目标 {target_index}：" if target_index else ""
            lines.append(f"{index}. {target_prefix}{item['name']}{suffix}")
            box = item.get("box")
            if isinstance(box, list) and len(box) == 4:
                lines.append(f"   位置：[{box[0]}, {box[1]}, {box[2]}, {box[3]}]")
            english_name = str(item.get("english_name") or "").strip()
            scientific_name = str(item.get("scientific_name") or "").strip()
            if english_name or scientific_name:
                alias_parts = [part for part in (english_name, scientific_name) if part]
                lines.append(f"   名称：{' / '.join(alias_parts)}")
            calorie = str(item.get("calorie") or "").strip()
            if calorie:
                lines.append(f"   热量：{calorie}")
            description = str(item.get("description") or "").strip()
            if description:
                lines.append(f"   简介：{description}")
            baike_url = str(item.get("baike_url") or "").strip()
            if baike_url:
                lines.append(f"   百科：{baike_url}")
        return "\n".join(lines)

    async def _recognize_message_image(
        self,
        msg_id: str,
        stream_id: str,
        endpoint: str,
        service_name: str,
        *,
        include_top_num: bool = False,
        include_dish_filter_threshold: bool = False,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """按消息 ID 提取图片并调用百度图像识别。"""

        _, image_base64 = await self._load_message_image(msg_id, stream_id)

        payload = await self._recognize_baidu_image(
            image_base64,
            endpoint,
            service_name,
            include_top_num=include_top_num,
            include_dish_filter_threshold=include_dish_filter_threshold,
        )
        return payload, self._serialize_results(payload)

    async def _recognize_hho_message_image(
        self,
        msg_id: str,
        stream_id: str,
    ) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """按消息 ID 提取图片并调用 HHo 懂鸟识别。"""

        image_format, image_base64 = await self._load_message_image(msg_id, stream_id)
        return await self._recognize_hho_bird_image(image_format, image_base64)

    def _ensure_tool_enabled(self, tool_name: str, display_name: str) -> None:
        """检查工具开关。"""

        if not getattr(self.config.tools, tool_name):
            raise RuntimeError(f"{display_name}工具当前未启用，请在插件配置的 [tools] 中打开 {tool_name}。")

    @Tool(
        "recognize_animal",
        description="查询某条消息中的图片是什么动物；当用户询问图片里的动物是什么时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_animal(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的动物。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_animal", "动物识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_ANIMAL_ENDPOINT,
                "动物识别",
                include_top_num=True,
            )
            return {
                "success": True,
                "content": self._format_results(results, "动物识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_animal 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"动物识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_bird",
        description="查询某条消息中的图片是什么鸟；当用户询问图片里的鸟类品种、鸟名或观鸟识别时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_bird(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的鸟类。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_bird", "鸟类识别")
            provider = self._get_bird_provider()
            annotated_image_sent = False
            if provider == "hho":
                image_format, image_base64 = await self._load_message_image(msg_id, stream_id)
                payload, results = await self._recognize_hho_bird_image(image_format, image_base64)
                provider_name = "HHo 懂鸟"
                fallback_title = "没有明显识别到鸟类，HHo 懂鸟的候选结果："
            else:
                payload, results = await self._recognize_message_image(
                    msg_id,
                    stream_id,
                    BAIDU_ANIMAL_ENDPOINT,
                    "动物识别",
                    include_top_num=True,
                )
                provider_name = "百度动物识别"
                fallback_title = "没有明显识别到鸟类，百度动物识别的候选结果："

            bird_results = [
                item
                for item in results
                if str(item.get("animal_class") or "").strip().upper() == "B"
                or _is_bird_name(str(item.get("name") or ""))
            ]
            if bird_results:
                content = self._format_results(bird_results, "鸟类识别结果：")
                if provider == "hho":
                    annotated_image_sent = await self._send_annotated_bird_image(
                        stream_id,
                        image_base64,
                        bird_results,
                    )
            else:
                content = self._format_results(results, fallback_title)
            return {
                "success": True,
                "is_bird_detected": bool(bird_results),
                "provider": provider,
                "provider_name": provider_name,
                "annotated_image_sent": annotated_image_sent,
                "content": content,
                "results": bird_results,
                "all_results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_bird 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"鸟类识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_plant",
        description="查询某条消息中的图片是什么植物；当用户询问图片里的花、草、树、植物名称或植物品种时使用。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_plant(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的植物。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_plant", "植物识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_PLANT_ENDPOINT,
                "植物识别",
            )
            return {
                "success": True,
                "content": self._format_results(results, "植物识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_plant 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"植物识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }

    @Tool(
        "recognize_dish",
        description="查询某条消息中的图片是什么菜品；默认未启用，只有配置打开后才用于识别食物、菜名、餐食和热量。",
        parameters=[
            _tool_param("msg_id", ToolParamType.STRING, "要识别的图片消息 ID", True),
        ],
    )
    async def handle_recognize_dish(
        self,
        msg_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """识别某条消息中的菜品。"""

        del kwargs
        try:
            self._ensure_tool_enabled("recognize_dish", "菜品识别")
            payload, results = await self._recognize_message_image(
                msg_id,
                stream_id,
                BAIDU_DISH_ENDPOINT,
                "菜品识别",
                include_top_num=True,
                include_dish_filter_threshold=True,
            )
            return {
                "success": True,
                "content": self._format_results(results, "菜品识别结果："),
                "results": results,
                "raw_response": payload,
                "target_message_id": msg_id.strip(),
            }
        except Exception as exc:
            logger.info("recognize_dish 调用失败：msg_id=%s error=%s", msg_id, exc, exc_info=True)
            return {
                "success": False,
                "content": f"菜品识别失败：{exc}",
                "target_message_id": msg_id.strip(),
            }


def create_plugin() -> BirdwatchingPlugin:
    """创建插件实例。"""

    return BirdwatchingPlugin()
