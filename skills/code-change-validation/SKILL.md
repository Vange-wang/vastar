---
name: code-change-validation
description: 锁定代码范围和原有改动，串行修复，在只读离线副本运行验证，用真实退出码和diff交付
version: "1.0.0"
triggers:
  - 代码修改验证
permissions:
  - A
  - B
required_tools:
  - read_file
  - search_text
  - stat_path
  - read_artifact
needs_memory: false
enabled: true
---

S06 使用默认关闭的 code_change。另行启用所需文件工具、start_process/read_process/terminate_process；
D级执行许可与每条命令的真实确认仍由原底座处理。Skill 激活不授予写入、安装或联网权限。

1. 先读问题相关文件，确认用户范围和验收目标。调用 lock，提供 change_id 和 definition：
   workspace=用户明确选定的现有checkout或worktree绝对路径；scope=逐个相对文件路径；
   acceptance=目标；checks=[{command: 只读离线pwsh验证命令, rationale: 为什么覆盖此改动}]。
   基线HEAD、完整可见dirty清单和diff由真实隔离进程读取，不能由模型填入。已有用户修改的目标拒绝写；
   不抹除/覆盖它，改用用户明确选择的干净隔离workspace，或另行人工合并。
2. workspace 必须已经存在。需要worktree时明确路径和起点，由外部授权操作建立；本工具不执行Git写命令。
   禁自动stash/clean、checkout .、宽泛restore/reset。清理前停所有在途进程并核实回执；
   若有.venv等联接，先核实它确为联接并只拆联接，再移除worktree，绝不递归跟随联接。
   本工具保留验证副本证据，不自动清理或提交。
3. 可先 verify(check序号) 得到改前命令结果；read_process读取真实输出，report核对完成状态。
   edit只委托原write_file/edit_file/move_file/delete_path，arguments中的path/dest须逐个属于scope。
   同一工作区串行写。不得以Skill绕开文件备份、权限或TOCTOU；生成文件不能混进scope。
4. 按影响选择必要测试/构建；格式微调不造镜像测试。不锁死实现字符串，测行为与可见输出。
   verify返回started只表示启动；须等report读取实际退出码和产物。每次重试显式调用、重新批准，最多12次。
   不安装依赖/联网。当前仅打包应用CPython标准库、PowerShell和已安装Git for Windows。
   Python命令可用 ..\python\python.exe -I -B；需第三方依赖或可写构建目录时如实标环境未满足。
   Git只用于独立的固定只读基线探针；任意测试命令的副本不含.git历史或Git运行程序。
   为兼容本机Git，隔离期间临时占用一个未使用盘符指向私有只读包，退出后按精确目标解除；不改变主机ACL。
5. 测试只接触冻结的只读离线副本，无真实DB/环境凭据。被排除的运行目录或敏感文件必须确被Git忽略，
   否则拒绝不完整快照。支持最多2048文件/64MiB源、128MiB Git元数据；不支持reparse源码/alternates。
   仅保留安全的core换行/大小写等Git设置；配置include、自定义filter属性及不支持的Git布局明确不可用。
   任意非scope文件/配置漂移、生成文件泄漏都会阻止交付。修改后旧验证hash失效。
6. report只按完成状态、退出码、快照hash及HEAD判定verified；模型说通过不能代替它。
   同命令基线绿而修改后红=疑似回归；基线也红=既有或环境问题；无基线=未分类，保留复现命令和输出。
   环境/探针失败不能当产品通过。ready_for_review仍须检查diff与验收目标，不是独立验收PASS。
7. 交付列明范围、base_revision、原dirty保留、实际命令/退出码/产物、diff、未解决项和限制。
   只逐个显式git add自己的文件，禁止目录/通配符；每层完成即提交。已发命令不能随代码回退撤销。
   关闭Skill不删证据，不影响普通对话；已启动任务先read/terminate再回退。
