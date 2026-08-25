"""定位本 skill 自己的 workspace/。

数据追踪在本产品里是自包含的：抓取脚本和 workspace/ 都住在同一个 skill 里
（scripts/ 是 skill 根目录下的一层）。产出（视频文件夹、账号数据/、reports/）
与 cookie 都在本 skill 根目录下，无需回指任何外部 skill。
"""
import os


def skill_root():
    """返回本 skill 根目录（scripts/ 的上一层）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def workspace_root():
    """返回本 skill 下的 workspace/ 绝对路径。"""
    return os.path.join(skill_root(), "workspace")


if __name__ == "__main__":
    print(workspace_root())
