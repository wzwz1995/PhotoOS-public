/**
 * PhotoOS · 使用说明（「说明」页的全部内容与渲染）
 *
 * ## 这一页回答三个问题
 *
 * 1. **第一次怎么用**：从导入到第一张精选，按真实顺序走一遍。
 * 2. **某件事在哪做**：按任务列入口，不让用户背功能名。
 * 3. **为什么做不到**：每个功能的限制写在它自己那一节里，不集中成一页免责声明。
 *
 * 内容按界面分节，而不是按代码模块分节：用户是先在某个页签里遇到问题，
 * 才会想知道原因。每节都从「在哪个页签做」开头，再写步骤，最后写边界。
 *
 * ## 为什么把限制留在正文里
 *
 * 含糊的限制等于没有限制。用户只有知道边界在哪，才不会把「搜不到」误当成
 * 「库里没有」，也不会因为一个做不到的事反复重试。限制不藏进弹窗。
 *
 * ## 零构建
 *
 * 原生 ES module。只依赖共享的转义工具；不从主应用模块取任何东西 ——
 * 导入它会在导入的同时把整个应用再启动一遍。
 */

import { escapeHtml } from './shared.js';

/** 内容版本标记：以后想知道「用户看的说明是不是最新的一版」，比对这个值即可。 */
export const HELP_VERSION = '2026-10-t';

/**
 * 第一次使用的四步。
 *
 * `where` 是界面上的页签名，用户能一眼对上号；`done` 是这一步的完成标志，
 * 让人知道自己没有卡在半路。
 */
const QUICKSTART = [
  {
    where: '维护',
    title: '把照片导入照片库',
    text: '打开「导入新照片」，选源目录。还没有照片库时，在「照片落在哪」填一个目录，'
      + '预演时会用它建立照片库。先点「预演」看城市、时段和重名冲突，确认无误再点「确认执行」。'
      + '照片是复制进库的，源文件不会被动。',
    done: '看到导入任务进入「后台任务」，状态变成「已完成」。',
  },
  {
    where: '维护',
    title: '等缩略图与分组跑完',
    text: '导入会同时生成缩略图、读取拍摄信息，并把照片整理成拍摄组与场景。'
      + '第一次大库可能要几分钟到几十分钟，进度在「后台任务」里。',
    done: '任务列表里没有还在排队或执行的导入任务。',
  },
  {
    where: '照片库',
    title: '确认照片能看',
    text: '回到照片库，在左侧选中刚导入的库。缩略图出现后点开一张，右边会显示拍摄信息、'
      + '完整路径和打开原片的入口。',
    done: '能打开一张照片，并且路径与画面一致。',
  },
  {
    where: '精选',
    title: '完成第一轮精选',
    text: '在精选里选一个场景，从第一张未审阅的开始。单张组按 Space 跳过，'
      + '相似组按 C 并排比较，按 2 标为精选。',
    done: '至少定下一张精选，剩下没看的可以下次接着来。',
  },
];

/**
 * 按任务找入口。
 *
 * 这不是功能目录，而是「我现在想干什么」的索引：同一个功能可能出现在多行里，
 * 因为用户眼里的任务本来就不按功能模块划分。
 */
const TASKS = [
  { label: '把新照片收进库', where: '维护 → 导入新照片', section: 'import' },
  { label: '从手机收照片', where: '维护 → 收件箱', section: 'inbox' },
  { label: '从手机批量拉取', where: '维护 → ADB', section: 'adb' },
  { label: '一场一场挑片', where: '精选', section: 'review' },
  { label: '按画面找某一类照片', where: '找照片 → 按画面内容', section: 'search-content' },
  { label: '按日期、地点找某次拍摄', where: '找照片 → 按时间 / 地点', section: 'search-when' },
  { label: '把脸归到人', where: '人物', section: 'people' },
  { label: '把照片收进长期主题', where: '主题', section: 'collections' },
  { label: '换盘、备份、完整性检查', where: '维护', section: 'maintenance' },
  { label: '清理已经标记的废片', where: '维护 → 废片清理', section: 'reject' },
];

const FAQ = [
  {
    q: '导入完成了，照片库还是空的？',
    a: '先确认左侧选中的是不是刚导入的库，再看看筛选条件有没有把照片排除。'
      + '如果库里有数量但网格是空的，到「维护 → 数据维护」跑一次完整性检查；'
      + '如果任务还在跑，等它结束再刷新。',
  },
  {
    q: '精选页没有场景，或者「按时间 / 地点」找不到？',
    a: '这两种浏览都以「场景」为单位，而场景要跑过分组才会生成。'
      + '到「维护 → 数据维护 → 命令（界面直跑）」执行一次「现在分组」，'
      + '或者在导入完成后确认后台任务已经跑完。',
  },
  {
    q: '找照片提示「这个词还没有准备过」？',
    a: '按画面找照片需要先算一次词向量。结果区会给出「立即准备」按钮，'
      + '点它等任务完成即可；也可以到「维护 → 数据维护 → 命令」里执行「准备这个搜索词」。',
  },
  {
    q: '人物页一个人也没有？',
    a: '人物页只展示已经算好的结果，不会在你打开页面时跑模型。'
      + '先到「维护 → AI 组件」安装 OpenCV 与人脸模型，再执行检测人脸、计算特征和聚类；'
      + '名字始终要由你来给。',
  },
  {
    q: '点照片打不开原图？',
    a: '最常见的原因是照片硬盘没接，或盘符变了。索引、缩略图和精选记录仍然可用；'
      + '插回硬盘后用「维护 → 离线与库状态」里的重绑定指回新位置，不需要重新导入。',
  },
  {
    q: 'PhotoOS 会移动、改名或删除我的照片吗？',
    a: '默认不会。导入是复制，扫描、索引、精选、主题、人物都只写数据库和派生文件。'
      + '唯一会动原片的是「废片清理」：只处理你明确标记的照片，移入 Windows 回收站，'
      + '且必须再次确认。',
  },
];

const GLOSSARY = [
  ['原片', '硬盘上的照片或视频文件本身。PhotoOS 默认只读。'],
  ['照片库', '登记到 PhotoOS 里的一个目录（通常是一块硬盘），以及它的索引。'],
  ['索引', '记录照片路径、拍摄信息、缩略图位置的数据库；它不是照片副本。'],
  ['拍摄组（Shot）', '同一时刻、同一机位的一组文件，例如一张 JPG 加一张 RAW。'],
  ['场景（Scene）', '一次拍摄，通常由若干个相邻的拍摄组组成，是精选的单位。'],
  ['精选状态', '未审阅、候选、精选、最终精选。由你标记，不代表照片好坏。'],
  ['主题', '你创建的长期作品集合，可以跨时间、地点和场景。'],
  ['表态', '在找照片里标「我要这种」或「不是这种」，用来调整下一次排序。'],
  ['数据根', '存放数据库、缩略图、预览和备份的目录，不在照片库里面。'],
];

/**
 * 各节内容。
 *
 * 字段含义：
 *   id     稳定英文 id（也是锚点名的来源，改动会让旧链接失效）
 *   tab    对应的界面标签名
 *   title  小节标题
 *   what   一两句话：这个功能解决什么问题
 *   how    用户实际要做的动作，按顺序
 *   limits 真实的限制与注意事项（做不到什么、代价是什么）
 *   cli    可选的等价命令行命令
 */
export const HELP_SECTIONS = [
  {
    id: 'library',
    tab: '照片库',
    title: '浏览与筛选',
    what: '照片库是浏览入口：按目录、时间、精选状态或主题翻看索引和缩略图。'
      + '看原图时才需要照片硬盘在线。',
    how: [
      '在左侧「照片库」里选一块硬盘；接了多块硬盘时先选对是哪一块。',
      '用「浏览」切换四个轴：文件夹 / 时间 / 精选状态 / 主题，再点一个具体范围。',
      '用「筛选」叠加条件：类型、状态、机型、年份、焦距、关键词，以及人物。',
      '筛选条件互相叠加（AND），不是互斥模式；条件条会列出当前生效的每一项。',
      '照片墙默认把一个拍摄组折成一张卡：标题写「2 个文件」，角标写类型；右上角「按拍摄组」可切回按文件。',
      '点开组卡：详情里的胶片条能切换同组每个文件；底部的「整组标记废片」一次作用于整组文件。',
      '点任意一张照片打开详情：拍摄信息、完整路径，以及「在资源管理器里显示」和「用浏览器打开原图」。',
      '详情里可以直接把这张加入主题，或新建一个主题并加入；加入的是整个拍摄组，不是单个文件。',
      '勾上「只看缺少元数据的」，把没读到拍摄信息的照片单独挑出来。',
      '网格默认每页 300 张，滚到底点「继续加载」；「每页」可改成 100 或 1000。',
      '筛乱了点「重置筛选」回到全库。',
    ],
    limits: [
      '「标签」浏览轴还没有启用：请求会明确返回 501，不会静默返回全库让你以为已经筛过了。',
      '筛选只能基于已经登记进索引的信息。没有拍摄时间的照片不会出现在按年份的结果里，用「只看缺少元数据的」把它们找出来。',
      '照片硬盘离线时，列表、缩略图、拍摄信息和精选记录都还能看；打开原图和资源管理器定位会失败，插回硬盘或重绑定后恢复。',
      '看不到缩略图时：到「维护 → 数据维护 → 命令」执行一次「索引新照片」，或者确认导入任务已经完成。',
      '折叠只改变照片墙的展示，不合并、不删除文件；精选与主题仍以拍摄组为单位，标一次就作用于整组。',
    ],
    cli: ['photoos stats', 'photoos scan', 'photoos index'],
  },
  {
    id: 'import',
    tab: '维护',
    title: '导入照片与后台任务',
    what: '把存储卡或暂存目录里的照片复制进照片库，并生成缩略图、拍摄信息和分组。'
      + '这是唯一会往库里写照片的动作，而且是复制，不是搬走。',
    how: [
      '打开「维护 → 导入新照片」，填源目录和目标照片库；还没有照片库时，在「照片落在哪」填目录，预演时会自动建库。源里有视频时还要选视频库。',
      '相机没定位、或境外坐标不在本地缓存里时，填「强制城市」——它会压过 GPS 判定。',
      '先点「预演」：看它会归到哪个城市、哪个时段，有没有重名冲突。',
      '确认落点后点「确认执行」；任务在下方「后台任务」里跑，可以暂停、恢复或取消。',
      '完成后回照片库刷新。导入结束会自动生成缩略图并重算分组，精选与「按时间」浏览马上就有内容。',
    ],
    limits: [
      '「预演抽样」只决定预演看多少个文件；点「确认执行」会处理源里的全部文件。',
      '源文件只被读取，不会被移动、改名或删除。导入是复制进照片库；什么时候清理源文件由你决定。',
      '参数改过就要重新预演一次，避免按旧的判断去写文件。',
      '没有定位又没填强制城市时，这批文件会归到「未知地点」。只有前后紧挨着、24 小时内城市一致的已定位拍摄才会被沿用。',
      '内容哈希要读全部原文件，外置机械盘上跑几十分钟到几小时都正常，期间不要拔盘。',
    ],
    cli: [
      'photoos import <源目录> --library <库>',
      'photoos import <源目录> --city <城市> --apply',
      'photoos jobs list',
    ],
  },
  {
    id: 'inbox',
    tab: '维护',
    title: '从手机导入（收件箱）',
    what: '手机不用连数据线：把照片复制到一个普通文件夹（收件箱），'
      + 'PhotoOS 等文件复制稳定后再导入。默认只读；也可以选择导入成功后清理已安全入库的副本。',
    how: [
      '展开「维护 → 从手机导入（收件箱）」，点「打开收件箱」，会打开或创建默认的收件箱文件夹。',
      '用资源管理器把手机里的照片复制进去，可以保留 DCIM\\Camera 这类子目录。',
      '点「扫描」：刚复制完的文件显示「复制中」；等它不再变化，状态会变成「可导入」。',
      '确认「可导入」的数量后点「立即导入」；它只处理已经复制完成的文件，剩下的可以下次再导。',
      '要腾空间就保持「导入成功后清理收件箱里已安全入库的副本」勾选；不想删就取消勾选。',
      '已经导入过、当时没勾选清理的，点「清理已入库…」；它只删能核对到库内目标的那部分。',
    ],
    limits: [
      '默认只读：不移动、不改名、不删除收件箱文件。勾选清理后，任务成功时才会删除台账可核对、目标仍在库里的副本；失败、目标缺失或文件已变化的都保留。',
      '清理只删收件箱里的副本，手机文件和照片库里的文件都不动；清理后会一并清掉对应台账条目。',
      '「复制中」不是故障：程序在等文件大小和修改时间稳定下来（默认 8 秒），避免把没复制完的文件收进库。',
      '导入过的文件会记在台账里，再扫也不会重复导入；同样的文件被重新复制一份（大小或时间变了）才会再处理。',
      '收件箱不能放在照片库目录里面。想换位置：在 config.json 里设 inbox_path，或设环境变量 PHOTOOS_INBOX_PATH。',
    ],
    cli: ['photoos import <收件箱> --library <库> --apply', 'photoos jobs list'],
  },
  {
    id: 'adb',
    tab: '维护',
    title: '从手机导入（ADB，可选）',
    what: '装了 Android Platform-Tools（adb）后，PhotoOS 可以直接从手机拉照片到收件箱。'
      + '任务卡片按已接收字节显示进度；手机上的文件只读，拉取失败也不会在照片库里留下半成品。',
    how: [
      '展开「维护 → 从手机导入（ADB，可选）」，在「设备」里选手机；只有一台时一般会自动选中。',
      '「手机目录」默认是 /sdcard/DCIM/Camera；只拉最近的，就填「只拉这天之后」。',
      '点「查询手机文件」先看一眼目录内容（只读，不下载）。',
      '点「拉到收件箱」并确认；任务在后台跑，可以在「后台任务」里看进度或取消。',
      '拉完后回「从手机导入（收件箱）」扫描，等状态变成「可导入」再点「立即导入」。',
    ],
    limits: [
      '手机要打开「开发者选项 → USB 调试」，第一次连接还要在手机上点「允许」。',
      '手机上的文件只被读取：不删除、不移动、不改名。取消拉取只会清掉本地暂存文件。',
      '只能读 /sdcard 或 /storage 下的目录；/data 这类应用私有目录会被拒绝。',
      '没装 adb 完全可以不用这一块，上面的收件箱照样能导入。找不到 adb 时，在 config.json 设 adb_path，或把它的目录加进 PATH。',
    ],
    cli: ['photoos jobs list'],
  },
  {
    id: 'review',
    tab: '精选',
    title: '逐场挑片',
    what: '系统把同一场景的照片按时间摆好，把连拍并成相似组。'
      + '你只需要对少数几组做决定，它不替你选。',
    how: [
      '在上面选一个场景。不知道选哪个，就用地点、拍摄日期或关键词把范围缩小。',
      '从第一张未审阅的开始。单张组按 Space 跳到下一张未审阅的。',
      '碰到相似组，按 C 并排比较，按 2 标为精选（1 是候选，3 是最终精选）。',
      '分组不对时按 V：勾选要拆出去的照片点「拆出为新组」，或点「与下一组合并」。',
      '想留一句为什么选它，按 R 写理由。',
      '前后翻按 ← →，跳过整组按 ↑，回到本组第一张按 ↓，退出按 Esc，快捷键总表按 ?。',
    ],
    limits: [
      '一个拍摄组里的 JPG/RAW 是同一个决定：标一次精选或最终，组内文件不会各标一次。',
      '第一轮的目标是一个场景 10 分钟内过完。大部分组只有一张，真正需要判断的通常只有十几个组。',
      '相似组按时间紧邻切分：能抓住连拍和同一机位的重复构图，抓不住「构图很像、拍摄时间隔得远」的照片。',
      '界面上的分组和数据库里的分组是两件事：改阈值界面立刻生效，要让数据也按新阈值重算，得跑一次「现在分组」。',
      '系统不评分、不推荐、不替你选。它只把有竞争的照片摆在一起，选哪张是你的判断。',
      '标错了可以退回：按 0 或 Backspace 回到未审阅。切换场景会从第一张未审阅的开始。',
      '跨场景的画面相似不会在这里合并；那种需求用「找照片 → 按画面内容」。',
    ],
    cli: [
      'photoos sessions backfill --apply',
      'photoos shots regroup --apply',
      'photoos scenes rebuild --apply',
    ],
  },
  {
    id: 'search-content',
    tab: '找照片',
    title: '按画面内容找照片',
    what: '用一句中文描述去找照片：它不把全库倒给你，而是把候选缩小到几个拍摄组，你再进去挑。',
    how: [
      '在「找照片」里选「按画面内容」。',
      '在下拉框里选一个预置搜索词。括号里的数字是这个词能找到的拍摄组数，不是照片张数。',
      '点「搜索」。结果按相似度排序，但排序不是好坏评价。找照片页默认只展示你确认过的「我要这种」；取消「只看我确认过的」才会显示全部候选。',
      '看结果卡片上的本地拍摄时刻：07:39 和 17:44 能分清日出与日落。',
      '每张卡片底下写着原片的文件夹与文件名，点文件名能看完整路径和大图。',
      '想留下这一批，点「结果存成主题」；想逐张挑，就去「精选」把对应场景过一遍。',
      '整页都对，就用页底的批量按钮一次标成「我要这种」；不必逐张点。',
      '下拉框里没有你要的词，就在输入框里打一个，按提示准备；准备完这个词会出现在下拉框里。',
      '新照片入库后如果顶部提示「还有 N 张没有画面向量」，点「管理关键词」：补画面向量是全局后台任务，补完后重算命中。',
      '工作台里勾选若干个词，点「重算选中候选」会按当前图片向量重算结果；它只更新候选池，不改词义，几秒完成。',
      '要加新词，直接在工作台的「添加并计算」里填关键词和实际 prompt；不用先回搜索框碰一次空态。',
      '点工作台每行的「发现」进入场景复核：前 3 轮每轮 40 张不确定 + 20 张确定，之后每轮 40 张确定；确定不足时用不确定补。',
      '场景复核里的「是 / 不是」只表示是不是这个场景，立即沉淀到场景；识别线滑杆控制候选范围。线太低会带进大量无关照片，界面会按已确认标签给出建议线。',
      '要让某个词更准，点它后面的「改」：编辑实际送进模型的 prompt，再重建这个词的向量与命中。',
    ],
    limits: [
      '只能搜预先算好的词。当前环境没有装模型运行时也算不了新词，这是刻意的取舍，不是功能坏了。',
      '「重算候选」不等于「补新照片向量」：前者只重算已有向量，后者要逐张跑 EmbeddingGemma，可能几十分钟到几小时；两者分开。',
      '补向量完成后旧词的结果不会自动变，还需要在工作台里重算候选；这是为了不让你在后台任务结束时被强制刷新。',
      '主搜索栏只提示“照片”缺向量；视频库缺 preview 的资产不会在这里显示成“照片缺预览”，详情在工作台的覆盖区。',
      '改 prompt 会替换这个词的命中快照，但不会删除照片、主题或你的表态。',
      '每个词的结果池最多存 200 组候选；表里显示「200+ 组」表示到了这个上限，不代表全库只有 200 组匹配。',
      '场景没有“确认找全”终态：新照片入库后还会继续参与下一轮；只有已确认的“是/不是”是稳定标签。',
      '输入没准备过的词，界面会明确说「这个词还没有准备过」并给出可以点或复制的准备命令，不会假装搜过，也不会给一个空结果。',
      '结果按拍摄组去重：同一组的 RAW 与 JPG 只占一格。卡片上的文件名是这一组的代表照片，要看全组请进精选。',
      '模型分不清日出与日落，也分不清相似的两种东西。请用本地拍摄时刻判断，不要只看排序。',
      '卡片角落的相似度只用来核对「它为什么排在前面」，不是评分，也不是「这张更好」的建议。',
    ],
    cli: [
      'photoos search 日出 --save',
      'photoos search --preset <你的概念清单> --min-ratio 1.20',
      'photoos embed --engine gemma --apply',
    ],
  },
  {
    id: 'search-edit',
    tab: '找照片',
    title: '调整搜索结果',
    what: '对结果表个态：这张我要，那张不是。整页会按你的表态重排，而且不会把你弹回列表开头。',
    how: [
      '一张一张表态：在卡片上点「我要这种」或「不是这种」，位置不会动。',
      '一次表态多张：点图片本身选中（卡片有蓝框和勾），按住 Shift 点可以选中到上一张之间的一片，'
        + '再用页面底部的操作条表态。',
      '整页结果会立刻重排，不用刷新。',
      '觉得改动太多，关掉「按我的修改重排」，就能看到模型原始的顺序。',
      '想推倒重来，点「清空修改」；它只忘掉你的表态，不删照片、不影响主题。',
    ],
    limits: [
      '卡片上的按钮只管这一张。要一次改多张，先点图片选中，再用底部操作条。',
      '标为「不是这种」的会立刻从结果里消失；标为「我要这种」的留在原位并加上金框。',
      '一次最多能表态 500 组。',
      '系统不是重新训练模型：它把你的选择算成一个平均方向，在原来的排序分上做加减。点得越多，结果越贴合你的判断。',
      '模型原始的顺序不会被覆盖，随时可以关掉重排看回原样。表态少于两次时不会重排。',
      '表态按搜索词分开记：在「日出」上的表态不会影响「婚纱」。两种表态都保存在库里，只有你点「清空修改」才会删掉。',
    ],
    cli: ['photoos embed --engine gemma --apply'],
  },
  {
    id: 'search-when',
    tab: '找照片',
    title: '按时间 / 地点 / 场次找',
    what: '这一层查目录和拍摄时间，不经过模型，所以结果是确定的：某一天、某个城市、某一次拍摄，就是那些。',
    how: [
      '在「找照片」里切到「按时间 / 地点 / 场次」。',
      '填条件：城市、场次关键词（目录名或日期）、从 / 到日期、最少拍摄组数，任意组合。',
      '点「筛选」。',
      '结果表里每一行都有「去精选这一场」：点一下直接进精选，开始逐张看。',
    ],
    limits: [
      '至少要给一个条件。一个条件都不给就等于全库，界面会直接提示。',
      '城市来自照片定位，或导入时你填的强制城市。没有定位也没有强制城市的照片，城市是空的，按城市筛不会带出它们。',
      '「最少拍摄组」筛的是这个场景一共有多少组，不是有多少张精选。',
      '场次关键词是模糊匹配目录名与城市名；写「2025」会撞上一整年，尽量写具体的日期或地名。',
    ],
    cli: ['photoos find --city 杭州 --date-from 2025-10-18'],
  },
  {
    id: 'people',
    tab: '人物',
    title: '把脸归到人',
    what: '把同一个人的脸归到一起，方便按人翻照片。名字永远由你给；'
      + '系统只会说「这里有一簇脸」，它不知道那是谁。',
    how: [
      '先在「维护 → AI 组件」装好 OpenCV 与人脸模型，再执行检测人脸、计算特征和聚类。这一页不跑模型。',
      '回到「人物」：左边是候选人物，没名字的都是模型提出来的，等你认。',
      '点开一个候选看脸墙：是他就点「是他」，也可以勾几张一起确认，然后填名字。',
      '认错的点「不是他」：那张脸会被排除在这个人之外，以后重算也不会再归回来。',
      '每张脸下面写着文件名，点文件名能看原片的完整路径与大图。',
      '确认几张之后点「重新学习全部」：它拿你确认过的脸去找更多，你确认得越多，它越准。',
      '模型提议的一批如果都对他，点「一键确认全部」；只有几张不确定就逐张点。',
      '混进来的非人脸（海报、雕像、屏幕里的脸）点「不是人」；点错了可以在左边撤销。',
      '一批脸其实都是另一个人：勾选它们，选目标人物，点「迁移」。',
      '想让这个人现在的照片进主题，点「加入主题」。',
    ],
    limits: [
      '模型只提出「这里有一簇脸」，不知道那是谁。没有名字的候选不能进主题。',
      '「不是他」会永久排除：被排除的脸不会再自动归到这个人名下，重算也一样；在「你否决过的」里可以撤销。',
      '「迁移」是真正的搬家：会从原来那个人名下移走，两边都会当场重算判断依据。',
      '「解散候选」只对还没命名的候选开放。脸一张都不会删，只是不再把它们当成一个人。',
      '侧脸、太小的脸、糊掉的脸在检测阶段就被跳过。这是漏检，不是分错；检测门槛可以在命令里调整。',
      '「加入主题」只改数据库关系：同名主题已存在就往里加，不存在才新建；不移动、不复制、不删除文件。',
    ],
    cli: [
      'photoos people detect --all',
      'photoos people embed --all',
      'photoos people discover',
      'photoos people name <候选ID> <名字>',
      'photoos people merge <源ID> --into <目标ID>',
      'photoos people learn',
    ],
  },
  {
    id: 'collections',
    tab: '主题',
    title: '长期作品集合',
    what: '主题是你自己建的长期作品集合：不受时间、地点、场次限制，'
      + '同一张照片可以同时属于多个主题。',
    how: [
      '在「主题」页点「新建主题」，填名称（例如「蓝调时刻」「西北游记」），描述可留空。',
      '点「添加照片」：从精选、硬盘上的目录，或另一个主题里挑。',
      '在主题里用 ↑ ↓ 调顺序，点「封面」把某张设为封面，点「移出」把某张移出去。',
      '要改名字或描述点「改名称 / 描述」；整个不要了就点「删除主题」。',
      '在精选里看到好照片，按 T 可以直接加进主题。',
      '要一次性把主题里的原片交出去：点「打包原片」。每一组取代表照片打成 zip，'
        + '产物路径会在完成后给出，不走浏览器下载。',
    ],
    limits: [
      '加入或移出只改数据库里的关系：不移动、不复制、不删除照片，也不改变精选状态。',
      '删除主题只删主题和成员关系；照片与精选记录不受影响（删除前会二次确认）。',
      '照片硬盘离线时，主题列表、封面缩略图和排序都还能用；原图打不开。',
      '打包是复制：原片不会被移动、改名或删除；包内附「清单.txt」写明每张的原路径与拍摄时刻。',
      '打包前会估算空间。几百张 RAW 可能几十 GB，慢是正常的；不在盘上的原片会写进清单，但不会进包。',
      '同一张照片在同一个主题里只会出现一次；一个拍摄组也只占一个位置，组内文件仍都在库里。',
      '主题顺序是你手动定的，不会自动重排。',
    ],
  },
  {
    id: 'maintenance',
    tab: '维护',
    title: '备份与换盘',
    what: '平时不用管，出问题时用这一页：查数据库、备份、看重复文件、换盘后恢复原图访问。',
    how: [
      '点「检查数据库完整性」：看数据库和索引记录是否一致，只报告，不自动改数据。',
      '点「立即备份数据库」：使用中也能备份，备份文件放在数据根下的 backups。',
      '点「重复内容报告」：按内容指纹列出重复文件和可回收空间，不移动也不删除。',
      '拔掉照片硬盘后：列表、缩略图、拍摄信息与精选记录照常可用，只是原图打不开。',
      '插回硬盘、盘符变了或换了硬盘：在「离线与库状态」里用重绑定指到新位置，先抽样校验，确认后生效。',
      '想知道数据放在哪儿：在「命令（界面直跑）」里跑一次 photoos config，它会打印数据根、数据库、缩略图、预览与日志目录。',
    ],
    limits: [
      '备份的是数据库（索引、精选、主题、人物关系），不包含原始照片；照片本身要另外备一份。',
      '重复内容报告只报告，不清理。要不要清理、怎么清理由你决定。',
      '重绑定会先抽样校验新位置，指错了会报不匹配，不会硬绑上去。',
      '拔盘期间，打开原图、重新提取元数据这类要读原文件的操作会失败；插回或重绑定后恢复。',
      '完整性检查只报告问题，不会自动修复；修复前先看结果，不要拿它当「一键修好」。',
    ],
    cli: [
      'photoos integrity',
      'photoos backup --keep 5',
      'photoos duplicates',
      'photoos library rebind <新位置> --dry-run',
    ],
  },
  {
    id: 'reject',
    tab: '维护',
    title: '废片清理',
    what: '把模糊、重复、拍坏的照片标出来，攒够了一次性清理。清理是移入 Windows 回收站，'
      + '可以从回收站还原，不是永久删除。',
    how: [
      '打开任意照片详情（照片库、找照片、人物、主题里点照片），点「标记为废片」；再点一次就是取消。',
      '标记只记在 PhotoOS 里，原片不动，也不会从照片库消失。',
      '到「维护 → 废片清理」看数量和样例，点「统一清理…」→「确认移入回收站」。',
      '清理任务跑完会自动重算分组；被移走的照片从照片库消失。',
    ],
    limits: [
      '只移入回收站，没有永久删除按钮；想彻底删掉请到 Windows 回收站里清空。',
      '命中精选或主题的照片会被跳过并计数，避免误删你选过的照片。',
      '清理是后台任务，可以取消；取消前已经处理的照片仍在回收站里。',
      '原片不在盘上（拔盘）时会跳过并报错，插回硬盘再清理。',
    ],
    cli: ['photoos jobs list'],
  },
  {
    id: 'privacy',
    tab: '维护',
    title: '数据与隐私',
    what: '说清楚照片和数据分别在哪、什么情况下会被改动，以及哪些事情永远不会做。',
    how: [
      '数据根里放数据库、缩略图、预览、备份与日志；原始照片仍然在它自己的硬盘上。',
      '想知道当前数据根：到「维护 → 数据维护 → 命令」跑一次 photoos config。',
      '想搬走或另存一份：把数据根整块复制走。原始照片本来就在你自己的硬盘上。',
      '想离线用：模型权重下载完之后，浏览、精选、找照片、分人都不需要联网。',
    ],
    limits: [
      '默认不改原片：只有「废片清理」会移动原片，且只移入回收站、只处理你标记过的照片。删掉数据库里的一条记录，不等于删掉照片文件。',
      '导入是复制；扫描、索引、精选、主题、人物关系都只写数据库和派生文件。',
      '数据库里不存盘符、绝对路径和用户名：每块硬盘用库标识加卷指纹记录，照片只记相对路径。换盘符或换机器不会让记录失效。',
      '模型都在本机跑，照片不会上传。唯一需要联网的是第一次下载模型权重。',
      '服务默认只监听本机地址，同一局域网里的其它设备访问不到。',
      '数据根里没有原片。要完整备份，数据库和照片硬盘都要备。',
    ],
  },
  {
    id: 'ai',
    tab: '维护',
    title: 'AI 组件',
    what: '按内容找照片、识别照片里的人物，要先装组件。运行时和模型权重装在数据根里，'
      + '装一次之后离线可用。不是所有功能都需要全部组件。',
    how: [
      '先看依赖：相似照片 / 以图搜图需要 ONNX Runtime（CPU）和 CLIP 视觉模型；'
        + '人物页需要 ONNX Runtime（CPU）、OpenCV 和人脸模型；'
        + '中文搜照片需要「中文语义搜索（EmbeddingGemma）」，它自带独立环境。',
      '在「维护 → AI 组件」里点「安装」。按钮旁会写体积，安装前会先确认。',
      '安装过程中该行下面会出现进度条；按钮变成「安装中…」时任务已经入队，看「后台任务」能看到阶段。',
      '下载源默认走国内镜像：Python 包用清华源，模型用 hf-mirror。地址可以改，改完点「测试」再保存。',
      '装完回对应页面：CLIP 装好就能在照片详情里找相似；人脸模型装好去「人物」页重建；'
        + 'Gemma 装完还要先导入照片并跑一次分组，再在「找照片」里准备搜索词。',
      '要腾空间就在组件行点「卸载」：它会删掉包与模型文件；安装失败留下的残渣用列表下方的「清理残留」。',
    ],
    limits: [
      '所有组件只在本机跑，照片不会上传；只有下载组件和模型时需要联网。',
      '体积与耗时是实数：ONNX Runtime 约 15 MB、CLIP 335 MB、OpenCV 55 MB、人脸 37 MB、'
        + '中文语义搜索约 2.7 GB（torch 1.0 GB + 模型 1.46 GB）。',
      'GPU 版 ONNX Runtime 约 3 GB，会替换 CPU 版；本机没有 CUDA 运行时时装完也只会回退到 CPU，失败会自动回滚。',
      '中文语义搜索装在用户数据目录下的独立环境里，不在安装包内；失败可以直接删掉重来。',
      '镜像只改变下载来源，不改变内容校验；模型文件按字节数与 sha256 复核，下载中断会从断点续传。',
      '组件显示「已安装」只代表文件就位；能不能搜、能不能认人，还取决于照片是否导入、分组是否跑过、搜索词是否准备过。',
    ],
    cli: ['photoos ai list', 'photoos ai fetch-model --engine all', 'photoos ai doctor'],
  },
];

/**
 * 把 HELP_SECTIONS 按使用阶段分组。
 *
 * 分组只决定目录和正文的阅读顺序，不改变每节的锚点；所以旧链接仍然有效。
 */
const HELP_GROUPS = [
  {
    id: 'start',
    title: '照片入库与浏览',
    lead: '先让照片出现在照片库里，再决定怎么挑。',
    ids: ['library', 'import', 'inbox', 'adb'],
  },
  {
    id: 'select',
    title: '挑选与整理',
    lead: '从一场拍摄里挑片，再按画面、时间、人物和主题找回来。',
    ids: ['review', 'search-content', 'search-edit', 'search-when', 'people', 'collections'],
  },
  {
    id: 'care',
    title: '维护与安全',
    lead: '换盘、备份、清理废片，以及照片到底存放在哪里。',
    ids: ['maintenance', 'reject', 'privacy', 'ai'],
  },
];

/** 锚点名：加前缀避免和页面上已有的元素 id 撞名。 */
function anchorOf(id) {
  return `help-${id}`;
}

function listItems(tag, values, className, label) {
  const items = values.map((text) => `<li>${escapeHtml(text)}</li>`).join('');
  return `<${tag} class="${className}" aria-label="${escapeHtml(label)}">${items}</${tag}>`;
}

function sectionById(id) {
  return HELP_SECTIONS.find((section) => section.id === id);
}

function tocGroup(title, links) {
  return `
    <div class="help-toc-group">
      <p class="help-toc-group-title">${escapeHtml(title)}</p>
      <ul class="help-toc-list">
        ${links.map((link) => `
          <li>
            <a href="${escapeHtml(link.href)}">
              ${link.tab ? `<span class="help-toc-tab">${escapeHtml(link.tab)}</span>` : ''}
              <span class="help-toc-title">${escapeHtml(link.title)}</span>
            </a>
          </li>`).join('')}
      </ul>
    </div>`;
}

function renderToc() {
  const featureGroups = HELP_GROUPS.map((group) => tocGroup(
    group.title,
    group.ids.map((id) => {
      const section = sectionById(id);
      return { href: `#${anchorOf(id)}`, tab: section.tab, title: section.title };
    }),
  )).join('');

  return `
    <nav class="help-toc" aria-label="说明页目录">
      <p class="help-toc-head">目录</p>
      ${tocGroup('开始', [
        { href: '#help-top', title: '从第一张照片开始' },
        { href: '#help-quickstart', title: '快速上手' },
        { href: '#help-tasks', title: '按任务找' },
      ])}
      ${featureGroups}
      ${tocGroup('附录', [
        { href: '#help-faq', title: '常见问题' },
        { href: '#help-glossary', title: '术语表' },
      ])}
    </nav>`;
}

function renderQuickStart() {
  return `
    <section class="help-block" id="help-quickstart">
      <header class="help-block-head">
        <h2>第一次使用：四步看到第一张精选</h2>
        <p>如果你刚装好 PhotoOS，按这个顺序走一遍。已经用起来的，直接从下面的任务索引查。</p>
      </header>
      <ol class="help-start">
        ${QUICKSTART.map((step, index) => `
          <li>
            <span class="help-start-n" aria-hidden="true">${index + 1}</span>
            <div class="help-start-body">
              <div class="help-start-head">
                <span class="help-where">${escapeHtml(step.where)}</span>
                <h3>${escapeHtml(step.title)}</h3>
              </div>
              <p>${escapeHtml(step.text)}</p>
              <p class="help-done"><span>完成标志</span>${escapeHtml(step.done)}</p>
            </div>
          </li>`).join('')}
      </ol>
    </section>`;
}

function renderTasks() {
  return `
    <section class="help-block" id="help-tasks">
      <header class="help-block-head">
        <h2>按任务找做法</h2>
        <p>下面按「你现在想做什么」列入口；点一行直接跳到对应说明。</p>
      </header>
      <ul class="help-task-list">
        ${TASKS.map((task) => `
          <li>
            <a href="#${anchorOf(task.section)}">
              <span class="help-task-do">${escapeHtml(task.label)}</span>
              <span class="help-task-where">${escapeHtml(task.where)}</span>
            </a>
          </li>`).join('')}
      </ul>
    </section>`;
}

function renderCli(section) {
  if (!section.cli || !section.cli.length) return '';
  return `
    <details class="help-cli-wrap">
      <summary><span>在终端执行（可选）</span><span class="help-cli-count">${section.cli.length} 条</span></summary>
      <pre class="help-cli" aria-label="等价的命令行命令">${escapeHtml(section.cli.join('\n'))}</pre>
    </details>`;
}

function renderSection(section) {
  return `
    <article class="help-section" id="${anchorOf(section.id)}">
      <header class="help-section-head">
        <span class="help-tab">${escapeHtml(section.tab)}</span>
        <h3>${escapeHtml(section.title)}</h3>
      </header>
      <p class="help-what">${escapeHtml(section.what)}</p>
      <h4 class="help-subhead">怎么做</h4>
      ${listItems('ol', section.how, 'help-how', '怎么做')}
      <h4 class="help-subhead is-limits">限制与注意</h4>
      ${listItems('ul', section.limits, 'help-limits', '限制与注意')}
      ${renderCli(section)}
    </article>`;
}

function renderReference() {
  return `
    <section class="help-reference" id="help-reference">
      <header class="help-block-head">
        <h2>功能详解</h2>
        <p>按页面分组。每一节先写怎么做，再写做不到什么；命令行放在最后，默认收起。</p>
      </header>
      ${HELP_GROUPS.map((group) => `
        <section class="help-group" id="help-group-${escapeHtml(group.id)}">
          <header class="help-group-head">
            <h2>${escapeHtml(group.title)}</h2>
            <p>${escapeHtml(group.lead)}</p>
          </header>
          ${group.ids.map((id) => renderSection(sectionById(id))).join('')}
        </section>`).join('')}
    </section>`;
}

function renderFaq() {
  return `
    <section class="help-block" id="help-faq">
      <header class="help-block-head">
        <h2>常见问题</h2>
        <p>先看这里；没有答案再去对应页面的「限制与注意」里找。</p>
      </header>
      <div class="help-faq">
        ${FAQ.map((item) => `
          <details>
            <summary><span>${escapeHtml(item.q)}</span></summary>
            <p>${escapeHtml(item.a)}</p>
          </details>`).join('')}
      </div>
    </section>`;
}

function renderGlossary() {
  return `
    <section class="help-block" id="help-glossary">
      <header class="help-block-head">
        <h2>术语表</h2>
        <p>界面里反复出现的几个词，先在这里对齐意思。</p>
      </header>
      <dl class="help-glossary">
        ${GLOSSARY.map(([term, desc]) => `
          <div>
            <dt>${escapeHtml(term)}</dt>
            <dd>${escapeHtml(desc)}</dd>
          </div>`).join('')}
      </dl>
    </section>`;
}

/** 当前滚动位置对应的目录项高亮。返回解绑函数。 */
function wireToc(container) {
  const view = container.closest('.view');
  const links = [...container.querySelectorAll('.help-toc a[href^="#help-"]')];
  if (!view || !links.length) return () => {};
  const targets = links
    .map((link) => ({ link, node: container.querySelector(link.getAttribute('href')) }))
    .filter((item) => item.node);
  let frame = 0;

  const update = () => {
    frame = 0;
    const top = view.getBoundingClientRect().top + 96;
    let active = targets[0];
    for (const item of targets) {
      if (item.node.getBoundingClientRect().top <= top) active = item;
    }
    for (const item of targets) {
      const current = item === active;
      if (current) item.link.setAttribute('aria-current', 'location');
      else item.link.removeAttribute('aria-current');
    }
  };
  const onScroll = () => {
    if (frame) return;
    frame = requestAnimationFrame(update);
  };

  view.addEventListener('scroll', onScroll, { passive: true });
  update();
  return () => {
    view.removeEventListener('scroll', onScroll);
    if (frame) cancelAnimationFrame(frame);
  };
}

/**
 * 把说明渲染进 container。
 *
 * 这一页是纯静态内容，一次 innerHTML 写入最省事；只要每个来自数据的字符串
 * 都过了 escapeHtml，就没有注入的余地。目录高亮是唯一的运行时行为，且只读 DOM。
 */
export function renderHelp(container) {
  if (!container) return;
  if (typeof container._helpCleanup === 'function') container._helpCleanup();

  container.innerHTML = `
    ${renderToc()}
    <div class="help-main">
      <header class="help-intro" id="help-top">
        <h2>从第一张照片开始</h2>
        <p class="help-intro-lede">
          PhotoOS 不替你选片。它把硬盘上的照片整理成可浏览、可筛选的索引，
          让你按场次挑，再按画面、时间、人物或主题找回来。
        </p>
        <p class="help-intro-note">
          这份说明按实际使用顺序写：<span>导入 → 分组 → 精选 → 找回来</span>。
          第一次用，从下面四步开始；已经用起来的，直接按任务查。
        </p>
        <div class="help-intro-actions">
          <a class="btn primary" href="#help-quickstart">开始第一次使用</a>
          <a class="btn" href="#help-tasks">按任务查找</a>
        </div>
      </header>

      <aside class="help-safety" aria-label="使用前需要知道的两条">
        <div>
          <strong>原片默认只读</strong>
          <p>导入是复制；扫描、索引、精选、主题、人物都只写数据库和派生文件。
            只有「废片清理」会移动原片，且只移入 Windows 回收站、只处理你标记过的照片。
            收件箱清理删除的是本地暂存副本，手机和照片库不受影响。</p>
        </div>
        <div>
          <strong>本机运行，可离线</strong>
          <p>照片、索引和缩略图都在你的机器上。模型下载完成后，浏览、精选、找照片、分人都不需要联网。</p>
        </div>
      </aside>

      ${renderQuickStart()}
      ${renderTasks()}
      ${renderReference()}
      ${renderFaq()}
      ${renderGlossary()}

      <p class="help-version">说明版本 ${escapeHtml(HELP_VERSION)}</p>
    </div>`;

  container._helpCleanup = wireToc(container);
}
