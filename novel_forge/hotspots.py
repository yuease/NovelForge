"""Cached public bestseller feeds and topic generation."""

import asyncio
import json
import logging
import math
from copy import deepcopy
from pathlib import Path

import httpx

from .llm import DEFAULT_MAX_TOKENS, LLMError, _resolve_credentials, call_llm, output_was_truncated, protected_output_budget
from .memes import _JsonCache, _http_client, _iso, _now_ms
from .providers import provider_requires_key
from .verifier import _llm_options, parse_json_from_llm


HOTSPOT_PLATFORMS = [
    "番茄小说",
    "红果短剧",
    "抖音短剧"
]

DEFAULT_FEED_URL = "https://top.baidu.com/api/board?platform=wise&tab=novel"

FALLBACK_FEED_URL = "https://top.baidu.com/api/board?platform=wise&tab=realtime"

HOOK_FORMULAS = [
    {
        "id": "hook_rebirth_slap",
        "name": "重生退婚/当场打脸公式",
        "platform": "番茄小说",
        "formula": "前世惨死/被背叛 -> 睁眼回到命运转折点 -> 当众撕破伪装反杀 -> 亮出底牌震慑全场",
        "example": "婚礼现场醒来，面对未婚妻的当众退婚，主角反手亮出百亿黑卡与家族覆灭证据。"
    },
    {
        "id": "hook_countdown_system",
        "name": "倒计时+高收益系统公式",
        "platform": "红果短剧",
        "formula": "绝境倒计时（10分钟内破产/死亡） -> 觉醒神级反差系统 -> 越不被看好越暴击返现 -> 强卡点断章",
        "example": "被赶出豪门只剩100元，激活「消费十倍返现」系统，第一分钟买下对方最想拍的古董。"
    },
    {
        "id": "hook_identity_reveal",
        "name": "隐瞒身份掉马反转公式",
        "platform": "抖音短剧",
        "formula": "主角隐藏顶级大佬身份低调生活 -> 反派狗眼看人低极限施压 -> 关键人物突然下跪/敬礼 -> 身份揭晓全场窒息",
        "example": "送外卖的青年被保安羞辱，下一秒全城首富带队冲出大门鞠躬喊先生。"
    },
    {
        "id": "hook_rule_horror",
        "name": "规则怪谈+唯一免疫公式",
        "platform": "番茄小说",
        "formula": "全员被拉入诡异规则副本 -> 别人只能战战兢兢遵守 -> 主角看见隐藏红字提示/拥有反向修改权 -> 反向收割诡异",
        "example": "规则说“午夜不能照镜子”，主角却看到镜子背面写着“照镜子可继承前任房主遗产”。"
    }
]

PACING_TEMPLATES = [
    {
        "id": "pace_tomato_fast",
        "name": "番茄黄金三章快节奏",
        "description": "第1章开篇即冲突（300字内入戏），第2章获得金手指/埋下核心伏笔，第3章完成首轮小高潮打脸并抛出更大悬念。",
        "beats": [
            "0-300字：危机/屈辱开局",
            "300-1000字：转折/金手指觉醒",
            "1000-2000字：首次反击与钩子"
        ]
    },
    {
        "id": "pace_hongguo_drama",
        "name": "红果短剧强卡点节奏",
        "description": "每600~800字一次情绪反转，对话占比40%以上，每章结尾必须卡在“真相即将揭晓”或“反派即将遭殃”的瞬间。",
        "beats": [
            "开场3秒钩子",
            "中段极限拉扯",
            "尾声高能卡点"
        ]
    },
    {
        "id": "pace_suspense_spiral",
        "name": "长线伏笔螺旋递进",
        "description": "主线爽点与暗线伏笔双线并行，每3~5章回收一个中小伏笔，同时抛出更高维度的真相碎片。",
        "beats": [
            "明线：阶段性破局",
            "暗线：信物/秘密线索递增",
            "大节点：全线收网"
        ]
    }
]

HOTSPOT_TROPES = [
    {
        "id": "trope_rebirth_tycoon",
        "title": "重生回到背叛夜，我反手买断仇家命脉",
        "platform": "番茄小说",
        "genre": "都市重生爽文",
        "heatScore": 98,
        "recommendedPov": "第三人称限知",
        "tags": [
            "重生",
            "商战复仇",
            "黄金三章",
            "强打脸"
        ],
        "hookFormula": "前世家破人亡 -> 重生回股权转让前夜 -> 截胡关键技术与人脉 -> 布局反杀",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "林渊前世被未婚妻与合伙人联手做局倾家荡产，睁眼重回十年前签署股权转让协议的当晚。他当场撕毁协议，凭借未来十年的金融记忆与老宅暗格中的神秘信物，反手做空仇家集团，开启逆袭之路。",
        "goldenThreeChapters": [
            "第1章：重生归来，鸿门宴上当众撕毁卖身契，反手录音留证逼退恶客。",
            "第2章：连夜赶回老宅密室取回先祖留下的核心账本与青铜信物，锁定第一桶金。",
            "第3章：次日开盘精准狙击仇家股票，对方资金链当场断裂，幕后黑手浮出水面。"
        ]
    },
    {
        "id": "trope_hongguo_fake_heiress",
        "title": "真千金归来：听见心声后全家跪求原谅",
        "platform": "红果短剧",
        "genre": "豪门虐渣爽文",
        "heatScore": 96,
        "recommendedPov": "第三人称限知",
        "tags": [
            "真假千金",
            "读心术",
            "全家火葬场",
            "强反转"
        ],
        "hookFormula": "真千金被污蔑赶出家门 -> 绑定心声外放/预知能力 -> 揭穿假千金阴谋 -> 豪门全家后悔莫及",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "沈清栀被认回豪门却遭假千金处处陷害，绝望之际意外觉醒“未来灾难弹幕”系统。她干脆摆烂断亲，偏偏全家人突然能听见她的心声，眼睁睁看着她精准避开所有商业陷阱并自立门户成为顶级大佬。",
        "goldenThreeChapters": [
            "第1章：家宴被栽赃偷窃项链，沈清栀冷笑断亲，内心弹幕剧透大哥今晚必遭车祸。",
            "第2章：大哥半信半疑避开死劫，全家震惊，沈清栀已凭前世记忆拿下城南地王。",
            "第3章：假千金联手外敌设局，沈清栀在拍卖会当众播放其罪证录音，全场哗然。"
        ]
    },
    {
        "id": "trope_apocalypse_hoard",
        "title": "极寒末世倒计时：我靠空间系统囤空百亿物资",
        "platform": "番茄小说",
        "genre": "末世囤货脑洞",
        "heatScore": 95,
        "recommendedPov": "第一人称",
        "tags": [
            "末世",
            "无限空间",
            "囤货",
            "杀伐果断"
        ],
        "hookFormula": "前世被圣母邻居分食 -> 重生回末世前30天 -> 变卖资产疯狂囤货 -> 打造安全堡垒看仇人冻毙门外",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "全球极寒冰封前一个月，我重生回到末世前夕，意外激活无限保鲜空间。我果断套现百亿资产，买空各大仓储超市、军工级发电机与恒温建材，将顶层豪宅改造成末日堡垒。极寒降临那天，前世害死我的白莲花邻居在门外哭求一口热汤。",
        "goldenThreeChapters": [
            "第1章：重生回极寒前30天，测试祖传吊坠开启千亩静止空间，直接挂牌抛售公司股份。",
            "第2章：包下整座冷链物流园与顶级安保改装团队，邻居嘲笑我疯了。",
            "第3章：暴雪提前降临，全城断水断电，我在恒温26度的客厅吃着火锅看监控。"
        ]
    },
    {
        "id": "trope_douyin_hidden_master",
        "title": "隐居外卖员竟是龙殿之主，老婆家族悔断肠",
        "platform": "抖音短剧",
        "genre": "都市战神赘婿",
        "heatScore": 94,
        "recommendedPov": "第三人称全知",
        "tags": [
            "隐藏大佬",
            "掉马",
            "极速打脸",
            "都市热血"
        ],
        "hookFormula": "隐姓埋名报恩 -> 岳家逼签离婚协议 -> 大人物齐聚门外下跪 -> 身份曝光震动全城",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "萧战为报救命之恩隐居小城送外卖，却在妻子生日宴上被岳母一家百般羞辱逼离婚。正当小人得意之时，百亿财阀总裁与战区统帅同时率队抵达酒店，单膝跪地恭迎龙主归位。",
        "goldenThreeChapters": [
            "第1章：送外卖赶赴妻子生日宴，被势利亲戚泼红酒并甩出离婚协议。",
            "第2章：萧战接通加密电话，一句“十分钟内到场”，全城首富连夜改道。",
            "第3章：酒店大门轰然推开，权贵齐跪高呼先生，反派当场吓瘫。"
        ]
    },
    {
        "id": "trope_infinite_rules",
        "title": "规则怪谈：只有我能看见隐藏的正确选项",
        "platform": "番茄小说",
        "genre": "无限流悬疑",
        "heatScore": 93,
        "recommendedPov": "第一人称",
        "tags": [
            "规则怪谈",
            "高智商",
            "微恐",
            "伏笔回收"
        ],
        "hookFormula": "卷入死亡怪谈 -> 纸条规则真假掺半 -> 主角看到红色批注 -> 反客为主收服诡异",
        "pacingTemplate": "pace_suspense_spiral",
        "samplePremise": "午夜十二点，整栋公寓被拉入《四号楼守则》怪谈世界。每个人门前都贴着七条致命规则，唯有我能看见规则背面用血字写着的“真正漏洞”。当其他租客在恐惧中违背规则惨死时，我已经拿着管理员钥匙敲响了诡异的房门。",
        "goldenThreeChapters": [
            "第1章：午夜停电，门缝塞进染血守则，我却看见每条规则旁都有系统批注的生路。",
            "第2章：利用规则漏洞反杀敲门诡，获得关键道具“四楼备用钥匙”。",
            "第3章：推开404室房门，发现整栋楼的幕后真相竟与十年前的失踪案有关。"
        ]
    },
    {
        "id": "trope_ancient_palace",
        "title": "嫡女重生：这届皇帝和权臣我都换掉",
        "platform": "红果短剧",
        "genre": "古言权谋宅斗",
        "heatScore": 92,
        "recommendedPov": "第三人称限知",
        "tags": [
            "古言重生",
            "宅斗",
            "强强联手",
            "朝堂博弈"
        ],
        "hookFormula": "前世满门忠烈被冤杀 -> 重生回及笄之年 -> 撕碎白莲花庶妹伪装 -> 联手腹黑世子掀翻棋盘",
        "pacingTemplate": "pace_suspense_spiral",
        "samplePremise": "将军府嫡女沈明月前世被渣男皇子与庶妹联手构陷，满门抄斩。重生回十五岁及笄礼当日，她一改温顺，当众揭穿庶妹私通外敌的证据，并主动向那位传闻中病弱狠戾的镇北王世子递出合作密信。",
        "goldenThreeChapters": [
            "第1章：及笄礼上庶妹故意落水栽赃，沈明月反手将计就计让其当众出丑。",
            "第2章：夜探书房截获通敌密信，与镇北王世子达成秘密盟约。",
            "第3章：渣男皇子登门求娶，沈明月当众退婚并亮出圣旨赐婚，打脸满堂。"
        ]
    },
    {
        "id": "trope_tomato_gaowu_bug",
        "title": "让你练基础锻体，你卡出十倍暴击成武神？",
        "platform": "番茄小说",
        "genre": "都市高武",
        "heatScore": 98,
        "recommendedPov": "第三人称限知",
        "tags": [
            "都市高武",
            "卡BUG系统",
            "反差打脸",
            "校园武考"
        ],
        "hookFormula": "武道垫底面临退学 -> 觉醒动作判定BUG系统 -> 广播体操刷成镇狱神拳 -> 考场一拳震碎测力碑",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "高三武道摸底考前三天，气血垫底的陆沉面临退学危机，却意外觉醒“武学判定漏洞系统”。只要动作标准度卡在微妙临界点，基础广播体操都能触发十倍暴击演化为镇狱神拳。考场上，所有富家子弟都在炫耀高阶武技，陆沉老老实实打出一套第八套广播体操，一拳把合金测力碑轰成碎渣。",
        "goldenThreeChapters": [
            "第1章：气血测试垫底被嘲讽退学，陆沉回家做拉伸却意外卡出系统暴击提示。",
            "第2章：通宵把军体拳与广播体操拆解刷满熟练度，气血值一夜飙破准武者线。",
            "第3章：正式武考当众开演广播体操，反派哄堂大笑之际一拳震碎SSS级测力碑。"
        ]
    },
    {
        "id": "trope_tomato_history_stream",
        "title": "刚登基想摆烂，天幕开始盘点十大昏君？",
        "platform": "番茄小说",
        "genre": "历史脑洞",
        "heatScore": 97,
        "recommendedPov": "第三人称限知",
        "tags": [
            "历史脑洞",
            "天幕直播",
            "迪化朝堂",
            "反套路"
        ],
        "hookFormula": "穿成傀儡新帝想摆烂 -> 苍穹降下全朝堂可见的史书天幕 -> 借天幕剧透精准铲除奸佞 -> 满朝文武脑补圣君深谋远虑",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "社畜赵珩穿越成内忧外患的傀儡小皇帝，刚打算交出玉玺保命摆烂，皇宫上空骤然展开巨型天幕：《盘点史上十大短命亡国君，第10名就是你！》。更绝的是弹幕把朝堂上谁是贪官、谁在密谋造反剧透得一清二楚。赵珩只好硬着头皮按弹幕点名抄家，结果满朝文武全员脑补陛下早有通天布局，跪求陛下赐教。",
        "goldenThreeChapters": [
            "第1章：登基大典刚想宣布休朝，天幕炸响开盘亡国倒计时，顺带点名丞相私通敌国。",
            "第2章：赵珩随手按弹幕坐标搜出密信，满朝震惊陛下竟早已暗中布网三年。",
            "第3章：天幕预告边关缺粮危局，赵珩照着弹幕配方拿出红薯高产图，百官泪目直呼天命。"
        ]
    },
    {
        "id": "trope_tomato_guyan_exile",
        "title": "抄家流放？我搬空皇宫国库去北境种田当女帝",
        "platform": "番茄小说",
        "genre": "古言脑洞",
        "heatScore": 97,
        "recommendedPov": "第三人称限知",
        "tags": [
            "抄家流放",
            "搬空库房",
            "随身空间",
            "基建女强"
        ],
        "hookFormula": "满门忠烈遭陷害流放 -> 激活无限灵泉仓储空间 -> 连夜搬空狗皇帝私库与贪官地窖 -> 荒蛮流放地吃香喝辣建成神城",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "大婚当日，镇国公府被狗皇帝一道圣旨抄家流放苦寒北境。林清晏睁眼觉醒百亿灵泉随身仓库，抄家官差还没进门，她先连夜把自家库房、仇家贪官地窖乃至皇宫私库搬空，一根毛都没给狗皇帝留。流放千里路上，别人啃观音土，她带全族在破庙里涮羊肉火锅，一路收流民、兴水利、锻神兵，直接把苦寒北境建成了让朝廷颤抖的铁血帝国。",
        "goldenThreeChapters": [
            "第1章：退婚抄家圣旨到门前，林清晏秒开灵泉空间把祖传宝库收得干干净净。",
            "第2章：夜潜皇宫与奸相府“零元购”，狗皇帝次日早朝发现国库空得能跑老鼠当场吐血。",
            "第3章：流放路上官差故意克扣干粮，林清晏半夜架起炭火炖肉香飘十里，官差跪求分口汤。"
        ]
    },
    {
        "id": "trope_tomato_xuanhuan_misunderstand",
        "title": "我只想安稳当个外门杂役，怎么全宗跪求我出山？",
        "platform": "番茄小说",
        "genre": "玄幻脑洞",
        "heatScore": 96,
        "recommendedPov": "第三人称限知",
        "tags": [
            "玄幻脑洞",
            "迪化误解",
            "苟道长生",
            "爆笑反差"
        ],
        "hookFormula": "只想当咸鱼苟到长生 -> 平平无奇日常举动被大佬过度解读 -> 魔教来袭刚想收拾包袱跑路 -> 敌人脑补绝世杀阵吓到自爆",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "陈默穿越修仙界，绑定“苟命就能长生”系统，打死不肯卷进宗门纷争，死守外门柴房当杂役。偏偏他随手砍柴的轨迹被剑宗圣女悟成太古剑意，熬糊的药渣让垂危太上长老当场破境飞升。直到魔教百万大军压境，陈默扛着扫帚准备连夜跑路，魔尊却看着他脚下的菜园子冷汗直流，以为踏入了诛仙绝阵，当场下令全军后撤三十里。",
        "goldenThreeChapters": [
            "第1章：宗门大比躲在后山劈柴，一斧劈偏却把路过圣女看傻，当场顿悟无上剑道。",
            "第2章：拿灵草熬了锅十全大补汤自己喝，太上长老偷喝一口残汤直接原地突破瓶颈。",
            "第3章：魔教圣使上门踢馆，陈默端着洗脚水泼出去，对方以为是化神毒雾吓得跪地求饶。"
        ]
    },
    {
        "id": "trope_tomato_niandai_70s",
        "title": "穿回七零被逼换亲？我靠手搓精密图纸进部委",
        "platform": "番茄小说",
        "genre": "年代",
        "heatScore": 95,
        "recommendedPov": "第三人称限知",
        "tags": [
            "七零年代",
            "技术流大女主",
            "换亲打脸",
            "强国科研"
        ],
        "hookFormula": "重生堂姐抢走所谓好亲事 -> 女主改嫁冷面铁血军官 -> 凭现代机械农技图纸解决卡脖子难题 -> 极品亲戚还在抢彩礼女主已进部委领奖",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "高级机械工程师苏念穿回1976年，睁眼就撞上重生堂姐哭闹抢亲——堂姐死活要嫁去省城厂长家当阔太太，把传闻中腿伤退役的冷面军官陆凛塞给苏念。苏念求之不得，远离极品亲戚后随军去了西北大厂，一边把外冷内热的兵王老公养得生龙活虎，一边随手画出高精度轴承与拖拉机改良图纸，直接被部委特招破格提干。",
        "goldenThreeChapters": [
            "第1章：堂姐当众闹换亲，苏念利落签字分家拿回母亲遗留工分与粮票。",
            "第2章：随军报到当天正赶上机械厂进口机床卡壳，苏念三分钟拆解测绘给出替代方案。",
            "第3章：堂姐在省城被厂长婆家折磨回娘家哭诉时，苏念的部委嘉奖大红花敲锣打鼓送到村口。"
        ]
    },
    {
        "id": "trope_tomato_xianshi_fortune",
        "title": "玄学主播在线算命：榜一大哥你身后站着三个",
        "platform": "番茄小说",
        "genre": "现言脑洞",
        "heatScore": 95,
        "recommendedPov": "第三人称限知",
        "tags": [
            "玄学直播",
            "算命破案",
            "灵异整活",
            "全网吃瓜"
        ],
        "hookFormula": "道观欠债开直播算命 -> 黑粉连麦当剧本托挑衅 -> 一眼看穿床底藏尸与豪门秘辛 -> 警方顺网线精准抓捕全网沸腾",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "清风观小师叔姜梨为还清道观修缮欠款，被迫开起直播算命，一卦五百。开播第一天，全网黑粉都当她是剧本诈骗团伙。结果第一个连麦炫富的网红刚露脸，姜梨淡淡开口：“你床底下那位女士已经憋了三天了，建议现在开门。”十分钟后警笛声响彻直播间，热搜直接爆掉，刑侦大队和豪门大佬排队抢着当她的榜一大哥。",
        "goldenThreeChapters": [
            "第1章：道观被催债断电，姜梨借邻居Wi-Fi开播，第一卦直接帮连麦黑粉床底找出通缉犯。",
            "第2章：顶流男星连麦炫耀真爱，姜梨反手算出对方未婚妻其实是三年前失踪案主谋。",
            "第3章：京圈首富半夜狂刷十个嘉年华求连线，姜梨瞥了眼画面：“别找了，你亲儿子在隔壁剧组跑龙套。”"
        ]
    },
    {
        "id": "trope_tomato_game_boss",
        "title": "游戏降临前三天，我先删号重练隐藏唯一职业",
        "platform": "番茄小说",
        "genre": "游戏体育",
        "heatScore": 94,
        "recommendedPov": "第三人称限知",
        "tags": [
            "网游融合现实",
            "重生先知",
            "唯一隐藏职业",
            "独狼无敌"
        ],
        "hookFormula": "重生回网游融合前72小时 -> 放弃满级号抢注被封禁BUG隐藏职业 -> 抢先单刷灾厄首杀 -> 现实降临当天一剑镇压全服",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "全球现象级全息网游《深渊纪元》公测三周年当晚，游戏数据将与现实彻底融合，怪物降临人间。前世惨死的林风重生回到融合前72小时，所有人都在疯狂冲刺老区战力榜，唯有他直接注销顶级大号，氪光全部家产卡进官方尚未修复的隐藏代码，抢注唯一神级职业“规则重构者”，在融合倒计时归零前单刷掉第一只世界级灾厄Boss。",
        "goldenThreeChapters": [
            "第1章：距离游戏融合还剩72小时，林风当着公会兄弟的面删掉满级大号并变卖全部资产。",
            "第2章：卡着凌晨服务器维护毫秒级漏洞，成功激活全服唯一隐藏职业“规则重构者”。",
            "第3章：抢在官方更新前单刷隐藏Boss爆出神级权柄，倒计时归零瞬间游戏降临，全球玩家抬头看见林风ID挂在苍穹榜首。"
        ]
    },
    {
        "id": "trope_tomato_urban_farming",
        "title": "辞职回村承包荒岛后，我的渔网捞出千年海兽与沉船",
        "platform": "番茄小说",
        "genre": "都市种田",
        "heatScore": 93,
        "recommendedPov": "第一人称",
        "tags": [
            "赶海种田",
            "山海图鉴",
            "治愈解压",
            "暴富日常"
        ],
        "hookFormula": "大厂裁员回海岛老家 -> 绑定山海图鉴与海神亲和 -> 赶海一网捞出天价野生黄唇鱼与古沉船 -> 昔日嘲讽前同事排队求购海鲜",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "30岁被互联网大厂裁员还背着房贷，我干脆回闽东海边老家承包了一座无人荒岛。本以为这辈子就守着破渔船晒太阳了，谁知下海第一天激活《山海渔猎图鉴》——潮汐退去别人只能捡几个花蛤，我随手一抄就是几十万一条的野生大黄鱼、会帮忙赶鱼进网的座头鲸幼崽，还有海底沉船里的古瓷器。很快，海岛小院成了全网顶流美食圣地，曾经嘲讽我的前总监托关系排队三个月只为吃一口我做的葱油青蟹。",
        "goldenThreeChapters": [
            "第1章：辞职回到海岛破屋，夜里赶海意外救下被渔网缠住的白海豚，激活《山海图鉴》。",
            "第2章：清晨退潮，图鉴金光一闪，在礁石缝里抄出重达八斤的顶级野生大黄鱼当场变现三十万。",
            "第3章：出海深潜追踪海底灵脉，一网捞出明代青花瓷船货，顺手把荒岛改造成全网爆火海景庄园。"
        ]
    },
    {
        "id": "trope_tomato_entertainment_anti",
        "title": "解约赔三千万？我上恋综靠发疯创飞全员顶流",
        "platform": "番茄小说",
        "genre": "星光璀璨",
        "heatScore": 94,
        "recommendedPov": "第三人称限知",
        "tags": [
            "娱乐圈发疯",
            "恋综整活",
            "嘴替爽文",
            "反套路爆红"
        ],
        "hookFormula": "被黑心公司当炮灰送进恋综 -> 索性撕掉剧本摆烂开麦 -> 现场拆穿绿茶影后与伪君子顶流 -> 靠发疯成了全网唯一活人顶流",
        "pacingTemplate": "pace_hongguo_drama",
        "samplePremise": "十八线小糊咖许夏被经纪公司逼着签下三千万天价违约金，还要在全网直播恋综里当绿茶女主的恶毒对照组。许夏直接摆烂：既然退圈也要赔钱，那我就不装了！直播第一天，绿茶影后装晕倒求男神抱，许夏一个箭步冲上去抢先拨通120并大喊“让一让我是学过心肺复苏的专业按压手”；顶流男神深情念土味情话，她当场掏出计算器核算对方税后片酬够买几斤排骨。结果全网观众笑到打鸣，许夏直接断层登顶热搜第一，公司越想雪藏，品牌方塞的代言费越高。",
        "goldenThreeChapters": [
            "第1章：公司逼签恶毒女配剧本，许夏进组后直接把台词本撕了垫桌脚，直播开麦吐槽天价盒饭。",
            "第2章：绿茶女主故意落水碰瓷男主，许夏掏出钓鱼竿把救生圈精准套在对方脖子上拖回岸边，弹幕笑疯。",
            "第3章：经纪人连夜打电话威胁解约封杀，许夏直接打开免提直播通话，全网热心网友众筹帮她凑违约金。"
        ]
    },
    {
        "id": "trope_tomato_suspense_occupational",
        "title": "夜班入殓师手记：我给禁忌遗体缝完皮，它替我挡了煞",
        "platform": "番茄小说",
        "genre": "悬疑脑洞",
        "heatScore": 95,
        "recommendedPov": "第一人称",
        "tags": [
            "民俗悬疑",
            "特殊职业",
            "入殓师",
            "长线伏笔"
        ],
        "hookFormula": "接手高薪夜班入殓师 -> 祖传针线看见死者遗愿与禁忌 -> 依规缝补禁忌遗体换取保命绝活 -> 直到送来一具写着自己名字的尸体",
        "pacingTemplate": "pace_suspense_spiral",
        "samplePremise": "为了筹齐妹妹的手术费，我应聘了城郊老殡仪馆月薪五万的夜班入殓师，入职守则第一条就是“凌晨三点后送来的无名尸，不管多碎都必须用红线缝完整，且绝不能看对方胸牌”。凭借祖传阴针手艺，我每缝好一具带着执念的特殊遗体，就能看清它生前最后三分钟的记忆，并换来一项替死挡煞的民俗绝活。直到第七个雨夜，冷库推进来一具被水泡得发胀的尸体，停尸单上赫然签着我自己的名字与生辰八字。",
        "goldenThreeChapters": [
            "第1章：签下高薪夜班保密协议，凌晨两点冷库送来第一具没有脸的无名红衣客，我按祖传针法缝出真容。",
            "第2章：在遗体衣领里摸出半枚刻着日期的民国铜牌，次日下班险遇车祸时，铜牌替我挡下一劫碎成两半。",
            "第3章：老馆长突然失踪只留下一本残缺的《夜班登记簿》，我翻到最后一页，看见今晚预约入殓的名字正是我自己。"
        ]
    },
    {
        "id": "trope_tomato_xianxia_support",
        "title": "修仙界唯一售后：专修各种金手指",
        "platform": "番茄小说",
        "genre": "玄幻脑洞",
        "heatScore": 97,
        "recommendedPov": "第三人称限知",
        "tags": [
            "玄幻脑洞",
            "职场反差",
            "修外挂",
            "反套路喜剧"
        ],
        "hookFormula": "别人带外挂穿越主角成天道售后 -> 满修仙界天命之子的金手指集体出Bug -> 上门排障收天价维修费与顶级法宝 -> 顺着异常日志查出天道底层暗手",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "别人穿越都带逆天系统，程序员出身的陆明却穿越成修仙界唯一的“天道售后工程师”，自带一把能看穿外挂源码的工单扳手。开局他就发现整个修仙界的天命之子全在出故障：退婚流废柴少年的戒指老爷爷突然蓝屏死机、气运丹师的聚宝盆疯狂卡顿吐废渣、重生女帝的记忆面板乱码成乱世菜谱。陆明干脆挂牌营业，专接各路大佬的金手指加急维修单——不靠苦修，靠修外挂收天价灵石、顶级法宝和人脉欠条，把传统玄幻套路全玩成喜剧梗，并在一次次奇葩排障里摸出天道频繁宕机的真相。",
        "goldenThreeChapters": [
            "第1章：退婚大典现场，废柴少年准备召唤戒指里的老爷爷反杀，戒指却突然冒烟报错死机，角落摆摊的陆明递出工单：加急排障，先付五百中品灵石。",
            "第2章：三息修好魂环回路救活老爷爷，少年当场翻盘并把半枚上古阵盘抵作维修尾款，“青石街专修外挂”的离谱口碑迅速在宗门暗网传开。",
            "第3章：杀气腾腾的重生女帝连夜踹开小店门，拍出一匣灵晶要求修复她突然乱码的前世记忆，陆明一扫报错日志却发现所有故障外挂都连着同一处天道后门。"
        ]
    },
    {
        "id": "trope_tomato_history_review",
        "title": "我给古人做测评，秦始皇连夜退货长生药",
        "platform": "番茄小说",
        "genre": "历史脑洞",
        "heatScore": 97,
        "recommendedPov": "第三人称限知",
        "tags": [
            "历史脑洞",
            "跨时空直播",
            "硬核测评",
            "古今反差"
        ],
        "hookFormula": "硬核测评博主手机连通历朝天幕 -> 专做古代骗局打假与现代商品测评 -> 化验拆穿长生丹重金属超标气炸始皇 -> 测评十元杂交稻与高产土豆让古代帝王集体求链接",
        "pacingTemplate": "pace_tomato_fast",
        "samplePremise": "现代硬核测评博主周野的手机意外连通古代各朝代天幕，但他不搞宏大叙事，而是专门做“现代商品测评”和“古代骗局打假”。开局他用现代化学实验和检测试剂当众拆穿徐福献给秦始皇的“长生丹”其实是汞铅严重超标的重金属毒丸，气得咸阳宫里的秦始皇当场拔剑追查炼丹房；下一期他又端出十块钱一斤的杂交水稻、高产脱毒土豆和两元一包的精制雪花盐现场测评，报出亩产与售价的瞬间，秦汉唐明各朝帝王集体破防刷屏求链接。",
        "goldenThreeChapters": [
            "第1章：周野开播硬核拆解“秦代长生丹配方”，用现代化学实验烧出水银蒸气并科普重金属中毒症状，刚准备服丹的秦始皇手一抖，当场怒拔佩剑点名召徐福入殿。",
            "第2章：徐福在殿前诡辩凡火难辨仙丹，周野次日加更古法丹砂重金属析出对比实验，铁证锤死骗局，始皇连夜下令退货查封炼丹房并坐等天幕下期更新。",
            "第3章：第二期《十块钱的现代平价神粮》上线，周野现场蒸熟杂交水稻米饭和拳头大的高产土豆并公布亩产数字，历朝帝王与农官集体破防追问如何求种。"
        ]
    }
]


_DEFAULT_SOURCE = "百度热搜小说榜"
_REFRESH_INTERVAL_MS = 30 * 60 * 1000


def _empty_feed():
    return {
        "feedMeta": {
            "sourceName": _DEFAULT_SOURCE, "sourceUrl": DEFAULT_FEED_URL, "boardDate": _iso(_now_ms(None))[:10],
            "lastSuccessAt": None, "lastAttemptAt": None, "cachedAt": None, "usingCache": False,
            "lastError": None, "feedVersion": 0, "itemCount": 0, "autoRefreshIntervalMs": _REFRESH_INTERVAL_MS,
        },
        "liveItems": [],
    }


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _array(value):
    return value if isinstance(value, list) else []


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _title(item):
    for key in ("title", "word", "name"):
        if item.get(key) is not None:
            value = _text(item[key])
            return value if value != "[object Object]" else ""
    return ""


def _read_feed(cached):
    cached = _mapping(cached)
    rows = cached.get("liveItems")
    if not isinstance(rows, list) or any(
        not isinstance(item, dict) or not _title(item)
        or (item.get("title") == "热点选题1" and "[object Object]" in str(item.get("summary", "")))
        for item in rows
    ):
        return None
    return {"feedMeta": {**_empty_feed()["feedMeta"], **_mapping(cached.get("feedMeta")), "usingCache": bool(rows)},
            "liveItems": rows}


def _flatten(nodes, depth=0):
    if depth > 5:
        return
    for item in _array(nodes):
        if not isinstance(item, dict):
            continue
        if _title(item):
            yield item
        else:
            nested = next((item[field] for field in ("content", "list", "items") if isinstance(item.get(field), list)), [])
            yield from _flatten(nested, depth + 1)


def extract_baidu_board_items(payload):
    data = _mapping(payload)
    cards = _mapping(data.get("data")).get("cards", data.get("cards"))
    return [item for card in _array(cards)
            for item in _flatten(_mapping(card).get("content", _mapping(card).get("topContent")))]


def _feed_items(payload):
    if isinstance(payload, list):
        return payload
    payload = _mapping(payload)
    for container in (payload, _mapping(payload.get("data"))):
        if isinstance(container.get("items"), list):
            return container["items"]
    return extract_baidu_board_items(payload)


def _number(value, default=0):
    try:
        number = float(value)
        if math.isfinite(number) and number:
            return int(number) if number.is_integer() else number
    except (TypeError, ValueError, OverflowError):
        pass
    return default


def _normalize_items(raw, source_name, source_url, board_date, attempt_at):
    normalized = []
    for item in _flatten(raw):
        index = len(normalized)
        title = _title(item)
        rank = _number(item.get("rank", item.get("index")), index + 1)
        descriptions = [_text(text) for text in _array(item.get("desc")) if _text(text)]
        author = _text(item.get("author"))
        display = _array(item.get("show"))
        if not author and display:
            author = _text(display[0])
        if not author:
            author = next((line[3:].strip() for line in descriptions if line.startswith(("作者：", "作者:"))), "")
        genre = _text(item.get("genre") or item.get("type") or item.get("category"))
        if not genre:
            genre = next((line.split("|")[0].strip() for line in descriptions if not line.startswith(("作者：", "作者:"))
                          and ("|" in line or len(line) <= 16)), "")
        genre = genre or "热门网文"
        raw_score = item.get("hotScore")
        score = _number(_mapping(raw_score).get("value", raw_score if raw_score is not None else item.get("hot")))
        tags = [tag for tag in _array(item.get("tags")) if _text(tag)] or [genre, "榜单热议"]
        summary = (_text(item.get("summary")) or " · ".join(descriptions) or _text(item.get("desc"))
                   or _text(item.get("content")) or f"{title}（{genre}）")
        normalized.append({
            "id": str(item.get("id") or f"live_{board_date}_{rank}_{index}"), "title": title, "author": author or "未知作者",
            "genre": genre, "rank": rank, "hotScore": score if score > 0 else max(60, 100 - index * 2),
            "sourceName": source_name, "sourceUrl": item.get("url") or item.get("sourceUrl") or source_url,
            "boardDate": board_date, "fetchedAt": attempt_at, "tags": tags, "summary": summary,
        })
        if len(normalized) == 30:
            break
    if not normalized:
        raise ValueError("解析榜单失败：未提取到有效标题条目")
    return normalized


class HotspotsFeed:
    def __init__(self, data_dir, *, client=None):
        self._cache = _JsonCache(Path(data_dir) / "hotspots_cache.json", _empty_feed, _read_feed)
        self.client = client

    async def refresh_hotspots_feed(self, *, source_url=DEFAULT_FEED_URL, custom_items=None, board_date=None,
                                    source_name=_DEFAULT_SOURCE, timeout_ms=10_000, now=None, client=None):
        attempt = _iso(_now_ms(now))
        date = board_date or attempt[:10]
        async with self._cache.lock:
            old = await self._cache.load()
            try:
                if custom_items is not None:
                    raw = custom_items
                else:
                    async with asyncio.timeout(timeout_ms / 1000):
                        async with _http_client(client if client is not None else self.client) as active:
                            response = await active.get(source_url, headers={
                                "User-Agent": "Mozilla/5.0 (compatible; NovelForge/1.0)",
                                "Accept": "application/json, text/plain, */*",
                            })
                            response.raise_for_status()
                            raw = _feed_items(response.json())
                            if not raw and source_url == DEFAULT_FEED_URL:
                                backup = await active.get(FALLBACK_FEED_URL)
                                if backup.is_success:
                                    raw = extract_baidu_board_items(backup.json())
                items = _normalize_items(raw, source_name, source_url, date, attempt)
                new = {"feedMeta": {
                    **old["feedMeta"], "sourceName": source_name, "sourceUrl": source_url, "boardDate": date,
                    "lastSuccessAt": attempt, "lastAttemptAt": attempt, "cachedAt": attempt, "usingCache": False,
                    "lastError": None, "feedVersion": _number(old["feedMeta"].get("feedVersion")) + 1, "itemCount": len(items),
                }, "liveItems": items}
                await self._cache.save(new)
                return {"ok": True, **deepcopy(new)}
            except (OSError, ValueError, httpx.HTTPError) as error:
                message = str(error) or "热点榜单请求超时"
                has_cache = bool(old["liveItems"])
                fallback = {"feedMeta": {**old["feedMeta"], "lastAttemptAt": attempt, "lastError": message, "usingCache": has_cache},
                            "liveItems": old["liveItems"]}
                if has_cache:
                    try:
                        await self._cache.save(fallback)
                    except OSError:
                        pass
                else:
                    self._cache.snapshot = fallback
                return {"ok": False, "usingCache": has_cache, "error": message, **deepcopy(fallback)}

    async def get_hotspots(self, *, platform="", genre="", query=""):
        saved = await self._cache.load()
        tropes, live = HOTSPOT_TROPES, saved["liveItems"]
        if platform and platform != "全部":
            tropes = [entry for entry in tropes if entry["platform"] == platform]
        if genre and genre != "全部":
            def in_genre(entry):
                return genre in (entry.get("genre") or "") or any(genre in tag for tag in _array(entry.get("tags")))
            tropes = [entry for entry in tropes if in_genre(entry)]
            live = [entry for entry in live if in_genre(entry)] or live
        query = query.strip().lower()
        if query:
            def matches(entry, summary):
                return any(query in str(value or "").lower() for value in (
                    entry.get("title"), entry.get("genre"), entry.get(summary), *_array(entry.get("tags")),
                ))
            tropes = [entry for entry in tropes if matches(entry, "samplePremise")]
            live = [entry for entry in live if matches(entry, "summary")]
        return deepcopy({
            "feedMeta": saved["feedMeta"], "liveItems": live, "fallbackTemplates": HOTSPOT_TROPES,
            "platforms": HOTSPOT_PLATFORMS, "tropes": tropes, "hookFormulas": HOOK_FORMULAS,
            "pacingTemplates": PACING_TEMPLATES,
        })


def _pick_sources(items, genre, limit=3):
    genre = genre.strip()
    ranked = sorted(items, key=lambda entry: bool(genre) and (
        (bool(entry.get("genre")) and (genre in entry["genre"] or entry["genre"] in genre))
        or any(genre in tag or tag in genre for tag in _array(entry.get("tags")))
    ), reverse=True)
    return ranked[:limit]


async def select_or_generate_concept(*, premise="", title="", genre="都市重生爽文", pov="第三人称限知",
                                     llm_config=None, prefer_freeform=False, meme_level="none",
                                     meme_source_mode="active_only", feed=None, meme_store=None, store=None,
                                     client=None, now=None):
    created = _iso(_now_ms(now))
    if premise.strip():
        return {
            "locked": True, "inspirationMode": "user_premise", "title": title.strip() or "作者自定创意项目",
            "premise": premise.strip(), "genre": genre, "pov": pov,
            "coreConflict": "围绕作者核心设定展开的主线冲突与破局挑战", "protagonistMotivation": "遵循作者设定的主角核心诉求",
            "goldenThreeChapters": [
                "第1章：按作者设定直接切入开篇冲突与主角处境",
                "第2章：展开关键线索或能力优势，确立短期目标",
                "第3章：完成首轮正面交锋并留下长线悬念",
            ],
            "mainPlot": premise.strip(), "sourceRefs": [], "candidates": [],
            "selectionReason": "作者已提供完整创意设定，系统直接锁定作者设定立项。", "createdAt": created,
        }

    mode, materials = "model_freeform", []
    if not prefer_freeform:
        cached = await feed.get_hotspots() if feed else {}
        live = cached.get("liveItems") or []
        if live:
            mode = "cached_feed" if cached["feedMeta"]["usingCache"] else "live_feed"
            materials = _pick_sources(live, genre)
        else:
            mode = "fallback_templates"
            materials = [{
                **{key: entry[key] for key in ("id", "title", "genre", "tags")},
                "hotScore": entry["heatScore"], "sourceName": "内置热点模板库", "sourceUrl": "",
                "boardDate": created[:10], "summary": entry["samplePremise"],
            } for entry in _pick_sources(HOTSPOT_TROPES, genre)]

    samples = []
    if meme_store is not None:
        if meme_level != "none":
            samples = await meme_store.get_meme_inspiration_samples(
                genre=genre, meme_source_mode=meme_source_mode, limit=3, now=now,
            )
        else:
            samples = (await meme_store.get_active_memes(now=now))[:2]
    supplement = ""
    if samples:
        lines = []
        for sample in samples:
            source = f"（来源：{sample['sourceUrl']}）" if sample.get("sourceUrl") else ""
            lines.append(f"- {sample.get('title')}：{sample.get('summary') or ''}{source}")
        supplement = "\n补充网感/热梗元素参考（仅借鉴反差或笑点结构，不生硬堆砌）：\n" + "\n".join(lines)
    hint = {
        "rich": "\n风格加权：方案需具备高浓度网感、强认知反差、高传播金句和强喜剧爽点。",
        "light": "\n风格加权：方案需兼具强剧情冲突与适度幽默吐槽/嘴替爽感。",
    }.get(meme_level, "")
    seed = materials[0] if materials else HOTSPOT_TROPES[0]
    request = f"""[TASK:TOPIC_CANDIDATES]
请基于以下信息生成 3 个高概念长篇网文候选选题方案（3个候选方案）：
题材：{genre}
视角：{pov}
灵感来源模式：{mode}
参考素材标题：{seed['title']}
参考素材列表：
{json.dumps(materials, ensure_ascii=False, indent=2)}{supplement}{hint}

要求每个候选方案包含：
title, premise, coreConflict, protagonistMotivation, goldenThreeChapters(3条数组), mainPlot, recommendedPov, tags, genreFit(0-100), novelty(0-100), longFormPotential(0-100)。
严格输出 JSON：{{ "candidates": [...] }}"""
    config = _llm_options(llm_config) if _mapping(llm_config).get("provider") else {"provider": "mock"}
    config["max_tokens"] = protected_output_budget(10_000, DEFAULT_MAX_TOKENS)
    candidates = []
    try:
        reply = await call_llm(
            **config, prompt=request, store=store, client=client, temperature=0.8,
            system="[TASK:TOPIC_CANDIDATES] 你是金牌网文选题策划总监，擅长从热点榜单提炼长篇连载高概念方案。只输出合法 JSON。",
        )
        parsed = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
        candidates = [item for item in _array(_mapping(parsed).get("candidates"))
                      if isinstance(item, dict) and _text(item.get("title")) and _text(item.get("premise"))]
    except LLMError as error:
        if error.code in ("ABORTED", "CIRCUIT_OPEN"):
            raise
        logging.getLogger(__name__).warning("选题模型调用失败，使用本地方案（%s）", error.code)
    if not candidates:
        candidates = [{
            "title": seed["title"][:10] + "：破局重生录", "premise": seed.get("summary") or HOTSPOT_TROPES[0]["samplePremise"],
            "coreConflict": "主角必须在势力围剿与时间倒计时下完成逆袭反杀。", "protagonistMotivation": "守护至亲并揭开隐藏多年的真相。",
            "goldenThreeChapters": HOTSPOT_TROPES[0]["goldenThreeChapters"], "mainPlot": "前期破局立威，中期多方博弈，后期收网揭秘。",
            "recommendedPov": pov, "tags": seed.get("tags") or [genre], "genreFit": 92, "novelty": 88, "longFormPotential": 90,
        }]

    scored = []
    for index, item in enumerate(candidates):
        total = (_number(item.get("genreFit"), 85) * 0.4 + _number(item.get("novelty"), 80) * 0.25
                 + _number(item.get("longFormPotential"), 85) * 0.35)
        scored.append({**item, "_index": index, "totalScore": math.floor(total + 0.5)})
    scored.sort(key=lambda item: item["totalScore"], reverse=True)
    winner = scored[0]
    explanation = (
        f"来源模式【{mode}】，综合题材匹配度({winner.get('genreFit', 90)})、差异度({winner.get('novelty', 88)})"
        f"与长篇展开空间({winner.get('longFormPotential', 90)})，以综合分 {winner['totalScore']} "
        f"从 {len(scored)} 个候选方案中自动选出《{winner['title']}》。"
    )
    refs = [{key: entry[key] for key in ("id", "title", "sourceName", "sourceUrl", "boardDate", "rank") if key in entry}
            for entry in materials]
    return deepcopy({
        "locked": True, "inspirationMode": mode, "title": title.strip() or winner["title"], "premise": winner["premise"],
        "genre": genre, "pov": winner.get("recommendedPov") or pov, "coreConflict": winner.get("coreConflict"),
        "protagonistMotivation": winner.get("protagonistMotivation"), "goldenThreeChapters": winner.get("goldenThreeChapters") or [],
        "mainPlot": winner.get("mainPlot") or winner["premise"], "tags": winner.get("tags") or [genre],
        "sourceRefs": refs, "candidates": scored, "selectionReason": explanation, "createdAt": created,
    })


async def generate_concept(*, trope_id="", platform="番茄小说", genre="", keywords="", llm_config=None, store=None, client=None):
    trope = next((entry for entry in HOTSPOT_TROPES if entry["id"] == trope_id), None) or next(
        (entry for entry in HOTSPOT_TROPES if entry["platform"] == platform), HOTSPOT_TROPES[0],
    )
    pacing = next((item for item in PACING_TEMPLATES if item["id"] == trope["pacingTemplate"]), PACING_TEMPLATES[0])
    config = _llm_options(llm_config)
    provider = config.get("provider")
    if provider and provider != "mock":
        creds = await _resolve_credentials(
            provider, config.get("base_url", ""), config.get("api_key", ""), config.get("model", ""),
            config.get("thinking_mode"), config.get("use_saved_connection", False), store,
        )
        if creds.api_key or not provider_requires_key(provider):
            try:
                reply = await call_llm(
                    **config, store=store, client=client, temperature=0.85,
                    system="你是番茄小说与红果短剧爆款策划总监，请输出合法 JSON，包含 title, premise, genre, pov, goldenThreeChapters(数组,3条)。",
                    prompt=(f"基于平台【{platform or trope['platform']}】、题材【{genre or trope['genre']}】、爆款套路【{trope['title']}】、"
                            f"关键词【{keywords or '、'.join(trope['tags'])}】，生成一个全新的高概念爆款故事设定与黄金三章钩子。"),
                )
                parsed = None if output_was_truncated(reply.get("finishReason")) else parse_json_from_llm(reply.get("text"))
                if isinstance(parsed, dict) and _text(parsed.get("title")) and _text(parsed.get("premise")):
                    return deepcopy({**parsed, "hookFormula": trope["hookFormula"], "pacingTemplate": pacing, "sourceTropeId": trope["id"]})
            except LLMError as error:
                if error.code in ("ABORTED", "CIRCUIT_OPEN"):
                    raise
                logging.getLogger(__name__).warning("灵感生成失败，使用本地模板（%s）", error.code)

    suffix = f"（融入核心元素：{keywords.strip()}）" if keywords.strip() else ""
    return deepcopy({
        "title": f"{trope['title'].split('，')[0]}：{keywords.strip()}" if keywords else trope["title"],
        "genre": genre or trope["genre"], "pov": trope.get("recommendedPov") or "第三人称限知",
        "platform": platform or trope["platform"], "premise": trope["samplePremise"] + suffix,
        "goldenThreeChapters": trope["goldenThreeChapters"], "hookFormula": trope["hookFormula"],
        "pacingTemplate": pacing, "sourceTropeId": trope["id"],
    })
