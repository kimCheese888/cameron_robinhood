# Telegram 改动审批 · `tg_approve.py`

> 2026-08-21 加的:策略/代码改动的确认环节可以搬到 Telegram 上做，不用非得在
> Claude Code 会话里等你回复。范围刻意收窄——见下面"不做什么"。
> 2026-09-04:改成按钮(用户反馈打字"确认"经常没用原生回复功能锚定，识别不到)。

## 流程

1. Claude 分析出一个具体改动(跟 T2 那次一样的流程：证据 + diff)，commit 到
   `pending/<slug>` 分支、push 到 GitHub（**不动 `main`**）。
2. 调 `tg_approve.propose(branch, summary)`：发一条 TG 消息，带 **✅ 确认 / ❌ 拒绝**
   两个按钮，记录这条消息的 `message_id` + 一个随机 token(绑定在按钮的
   `callback_data` 里)到服务器上的 `.pending_proposals.json`。
3. 你在 TG 里**点按钮**：
   - 点「✅ 确认」→ 批准，按钮消失，弹一个"已确认，准备部署"的小提示
   - 点「❌ 拒绝」→ 否决，分支不合并，按钮消失
   - (备用)不想点按钮也可以像以前一样**原生回复**那条消息打「确认」/「不」，
     两条路都认
4. Claude 定时(cron 唤醒)调 `tg_approve.check_approvals()` 轮询，看到批准就
   `git merge` 那个分支到 `main`、push、服务器 `git pull` + 重启服务，然后
   `notify_done()` 回一条"已上线"。否决就什么都不做，分支留着不合并。

## 为什么这么设计(安全边界)

- **只认按钮点击(或锚定的原生回复)，不认平铺文字**：`check_approvals()` 只处理
  `callback_query`(按钮点击，`callback_data` 里的 token 必须匹配某条待批准
  消息)或原生 reply 精确匹配固定词——聊天里随口说的话、点错按钮、回复错消息、
  关键词不对，全部忽略。这不是一个能在 TG 里打字下命令的通道，只是对**已经
  写好的具体 diff**的"是/否"确认。
- **批准动作本身不跑 git/部署命令**——`check_approvals()` 是纯只读的轮询，
  返回批准了哪些分支；真正 merge/push/重启还是由 Claude 会话来做，跟这个项目
  一直以来的改动流程一样，只是"人工确认"这一步挪到了 TG。
- **改动先进 `pending/*` 分支，不直接进 `main`**：即使 Telegram 那边出问题
  (bot token 泄露、账号被盗、误触发)，最坏情况是一个未审查的分支被合并——
  仍然会留下清晰的 git 记录可以立刻 revert，不是绕过 git 直接改服务器文件。
- **前提**：TELEGRAM_BOT_TOKEN 得是没泄露过的——这次会话早前有一次 token
  被直接贴在聊天里，如果还没在 @BotFather 里 `/revoke` 换新的，这个通道的
  安全性就无从谈起，先确认换过。

## 不做什么(明确排除)

- 不接受 Telegram 里的自由指令("把 XX 参数改成 YY")——那等于允许聊天软件
  直接操控生产代码，攻击面太大，用户已经选择了"仅确认已提案方案"这个更窄
  的版本。
- 不做无人值守的自动合并——批准之后还是需要 Claude 会话被唤醒去执行部署，
  不是 webhook 秒触发；`check_approvals()` 得靠 cron 轮询发现（受限于同样
  没有公网入口，无法做 Telegram webhook）。

## 用法

```bash
# 提案
.venv/bin/python tg_approve.py propose "pending/t9-fade-filter" "T9: 加衰竭过滤..."

# 轮询(通常是定时唤醒里跑)
.venv/bin/python tg_approve.py check
```
