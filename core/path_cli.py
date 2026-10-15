"""路径维护命令无需模型/密钥/终端 UI；预览默认只读。"""
import uuid
from pathlib import Path

from core.path_context import PathContextError, user_config_dir
from core.path_migration import format_result, initialize_project, migration_preview
from memory.migrations.engine import migrate, preview


def add_path_arguments(parser):
    parser.add_argument("--project", help="已初始化项目根目录")
    parser.add_argument("--data-dir", help="独立数据目录（须与项目身份匹配）")
    parser.add_argument("--cwd", help="工具相对路径的操作起点")
    parser.add_argument("--workspace", help="显式执行副本根；不创建副本")
    parser.add_argument("--init-project", action="store_true", help="显式创建新项目与空库后退出")
    parser.add_argument("--migrate-from", help="旧安装目录；默认仅预览，不修改历史数据")
    parser.add_argument("--apply-migration", action="store_true", help="按本次参数备份并一次性导入；先停止旧进程")
    parser.add_argument("--show-paths", action="store_true", help="只读显示实际路径与配置来源后退出")
    parser.add_argument("--migrate-storage", help="数据库 schema 迁移；默认只读预览")
    parser.add_argument("--apply-storage-migration", action="store_true", help="先备份再执行 schema 迁移")


def handle_maintenance(args):
    if args.migrate_storage or args.apply_storage_migration:
        if not args.migrate_storage:
            raise PathContextError("--apply-storage-migration 需要 --migrate-storage")
        if any((args.project, args.init_project, args.migrate_from, args.apply_migration,
                args.config, args.cwd, args.workspace, args.continue_session, args.data_dir, args.show_paths)):
            raise PathContextError("数据库 schema 维护命令不能与项目/运行参数混用")
        result = migrate(args.migrate_storage) if args.apply_storage_migration else preview(args.migrate_storage)
        print(format_result(result))
        return True
    if not (args.init_project or args.migrate_from or args.apply_migration):
        return False
    if not args.project:
        raise PathContextError("初始化/迁移需要显式 --project")
    if args.apply_migration and not args.migrate_from:
        raise PathContextError("--apply-migration 需要 --migrate-from")
    if args.init_project and args.migrate_from:
        raise PathContextError("新建与迁移互斥")
    if args.config or args.cwd or args.workspace or args.continue_session:
        raise PathContextError("维护命令不接受 --config/--cwd/--workspace/--continue；先初始化再配置")
    if args.migrate_from and not args.data_dir:
        raise PathContextError("迁移预览/执行须指定同一 --data-dir")
    data = Path(args.data_dir) if args.data_dir else user_config_dir() / "projects" / uuid.uuid4().hex
    if args.migrate_from and not args.apply_migration:
        result = migration_preview(args.migrate_from, args.project, data)
    else:
        result = initialize_project(args.project, data, source_app=args.migrate_from)
    print(format_result(result))
    return True
