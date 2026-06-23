"""
skill_manager.py
技能目录管理模块
功能：扫描skills目录、解析SKILL.md的yaml前置元数据、加载技能内容、查看技能列表
依赖：config.py
"""
import yaml


# 从全局配置导入路径
from config import SKILLS_DIR,SKILL_REGISTRY

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """
    解析Markdown文件顶部YAML前置元数据 --- yaml内容 --- 正文
    返回：(元数据字典, 正文内容)
    """
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    return meta, parts[2].strip()


def scan_skills():
    """全盘扫描skills文件夹，刷新技能注册表，程序启动自动执行"""
    SKILL_REGISTRY.clear()
    if not SKILLS_DIR.exists():
        return
    # 遍历子文件夹（一个文件夹对应一个技能）
    for directory in sorted(SKILLS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        manifest_file = directory / "SKILL.md"
        if not manifest_file.exists():
            continue
        raw_content = manifest_file.read_text(encoding="utf-8")
        meta, _ = _parse_frontmatter(raw_content)
        skill_name = meta.get("name", directory.name)
        skill_desc = meta.get("description", raw_content.split("\n")[0].lstrip("#").strip())
        SKILL_REGISTRY[skill_name] = {
            "name": skill_name,
            "description": skill_desc,
            "full_content": raw_content,
        }


def list_skills() -> str:
    """工具函数：返回格式化的全部技能列表文本，供模型调用"""
    if not SKILL_REGISTRY:
        return "(no skills found)"
    lines = []
    for skill_info in SKILL_REGISTRY.values():
        lines.append(f"- {skill_info['name']}: {skill_info['description']}")
    return "\n".join(lines)


def load_skill(name: str) -> str:
    """工具函数：根据技能名称读取完整技能文档内容"""
    skill = SKILL_REGISTRY.get(name)
    if not skill:
        available_names = ", ".join(SKILL_REGISTRY.keys()) or "(none)"
        return f"Skill not found: {name}. Available skills: {available_names}"
    return skill["full_content"]


# 程序初始化自动扫描加载所有技能
scan_skills()