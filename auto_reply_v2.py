# -*- coding: utf-8 -*-
"""
抖音多账号自动回复 v22 — 仅陌生人 + 退出生成报告
====================================================
流程：
  1. 扫码登录 → 跳转私信页 → 检测「陌生人消息」→ 点击进入
  2. 停留在陌生人列表 → 检测红点 → 自动回复
  3. 退出时 → 弹出提示 → 返回主私信页 → 抓取每个陌生人的回复(手机号等)
  → 导出Excel到桌面
"""
import os, sys, re, json, time, csv, threading
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QFileDialog, QGroupBox, QCheckBox, QStatusBar
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5.QtGui import QFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(BASE_DIR, "chrome_profiles")
CONFIG_FILE = os.path.join(BASE_DIR, "rules.json")
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
POLL = 5
DESKTOP = os.path.expanduser("~/Desktop")

os.makedirs(PROFILES_DIR, exist_ok=True)


def load_rules():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": [
        {"name": "账号1", "enabled": True,
         "reply_text": "请问您是遵义市哪个区县的户口呢？如需帮助请留下☎️"}
    ]}


def save_rules(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def find_chrome():
    if sys.platform == "win32":
        for p in [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            os.path.expandvars("%LOCALAPPDATA%\\Google\\Chrome\\Application\\chrome.exe"),
        ]:
            if os.path.exists(p): return p
    return None


def get_driver_path():
    exe = "chromedriver.exe" if sys.platform == "win32" else "chromedriver"
    local = os.path.join(BASE_DIR, exe)
    if os.path.exists(local): return local
    import shutil
    return shutil.which(exe)


class AccountWorker(QThread):
    log_signal = pyqtSignal(str, str)
    status_signal = pyqtSignal(str, str)
    report_ready = pyqtSignal(str, str)  # account_name, filepath

    def __init__(self, acc, idx):
        super().__init__()
        self.acc = acc
        self.idx = idx
        self.name = acc["name"]
        self.reply_text = acc.get("reply_text", "")
        self.poll = POLL
        self._stop = False
        self._login_ok = threading.Event()
        self._export_now = threading.Event()  # 手动导出触发
        self._driver = None
        # 今天回复过的所有陌生人: {昵称: {"first_msg": 对方第一条消息, "my_reply": 我方回复}}
        self.today_strangers = {}
        self._in_stranger = False
        self._export_path = None
        self._export_done = threading.Event()

    def confirm_login(self):
        self._login_ok.set()

    def trigger_export(self, path=None):
        self._export_path = path
        self._export_done.clear()
        self._export_now.set()

    def stop(self):
        self._stop = True

    def log(self, msg):
        self.log_signal.emit(self.name, msg)

    def _clean_name(self, raw):
        """去掉时间后缀：刚刚/分钟前/小时前/昨天/HH:MM/月日"""
        return re.sub(
            r'(刚刚|\d+分钟前|\d+小时前|昨天|\d{1,2}:\d{2}|\d{1,2}月\d{1,2}日|\d{2}/\d{2})$',
            '', raw
        ).strip()

    def run(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError:
            self.log("缺少 selenium"); return

        chrome = find_chrome()
        if not chrome:
            self.log("未找到 Chrome"); return

        profile = os.path.join(PROFILES_DIR, f"acc_{self.idx}")
        os.makedirs(profile, exist_ok=True)

        opts = Options()
        opts.binary_location = chrome
        opts.add_argument(f"--user-data-dir={profile}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("detach", True)

        drv = get_driver_path()
        svc = Service(executable_path=drv) if drv else Service()
        try:
            driver = webdriver.Chrome(service=svc, options=opts)
        except Exception as e:
            self.log(f"Chrome启动失败: {e}"); return

        self._driver = driver
        driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        driver.set_window_size(500, 800)

        try:
            driver.get("https://www.douyin.com")
            self.log("请扫码登录，完成后点「确认已登录」")
            self.status_signal.emit(self.name, "等待登录")

            while not self._login_ok.is_set() and not self._stop:
                time.sleep(0.5)
            if self._stop: return

            # 跳转到聊天页
            driver.get(CHAT_URL)
            time.sleep(3)
            self.log("等待10秒后刷新页面...")
            time.sleep(10)
            driver.refresh()
            time.sleep(3)
            self.status_signal.emit(self.name, "监控中")
            self.log("等待陌生人消息...")
            last_reply_time = {}
            last_refresh = time.time()
            REFRESH_INTERVAL = 30  # 首页每30秒刷新

            while not self._stop:
                # 手动导出触发
                if self._export_now.is_set():
                    self._export_now.clear()
                    self._in_stranger = False
                    fp = self._generate_report(filepath=self._export_path)
                    self._export_path = None
                    self._export_done.set()
                    last_refresh = time.time()  # 重置刷新计时，避免导出后立即刷新
                    if fp:
                        self.log(f"导出: {os.path.basename(fp)}")
                    continue

                # ─── 不在陌生人列表内：检测+定期刷新 ───
                if not self._in_stranger:
                    if time.time() - last_refresh > REFRESH_INTERVAL:
                        self.log("刷新页面...")
                        driver.refresh()
                        time.sleep(3)
                        last_refresh = time.time()
                    
                    if self._enter_stranger(driver):
                        self._in_stranger = True
                        self.log("已进入陌生人消息，停止刷新")
                        last_reply_time = {}
                    else:
                        time.sleep(self.poll)
                        continue

                # ─── 在陌生人列表内：验证+只回复第一个 ───
                # 验证：右侧出现陌生人列表容器
                still_in = driver.execute_script("""
                    let list = document.querySelector('[class*="conversationStrangerConversationListlist"]');
                    if (!list) return false;
                    let items = list.querySelectorAll('[class*="conversationConversationItemwrapper"]');
                    return items.length > 0;
                """)
                if not still_in:
                    self._in_stranger = False
                    self.log("不在陌生人列表，重新检测...")
                    continue

                # 找第一个陌生人（陌生人列表容器内）并点击，返回干净昵称
                clicked = driver.execute_script("""
                    let list = document.querySelector('[class*="conversationStrangerConversationListlist"]');
                    if (!list) return '';
                    let items = list.querySelectorAll('[class*="conversationConversationItemwrapper"]');
                    if (items.length === 0) return '';
                    let first = items[0];
                    let title = first.querySelector('[class*="conversationConversationItemtitle"]');
                    let name = title ? title.textContent.trim() : '';
                    first.focus();
                    ['mousedown','mouseup','click'].forEach(e =>
                        first.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                    );
                    return name;
                """)

                if not clicked:
                    time.sleep(self.poll)
                    continue

                first_name = self._clean_name(clicked)
                if not first_name:
                    time.sleep(self.poll)
                    continue
                now = time.time()
                if first_name in last_reply_time and now - last_reply_time[first_name] < 30:
                    time.sleep(self.poll)
                    continue

                self.log(f"回复: {first_name}")
                time.sleep(2)

                # 读对方第一条消息 (TextMessageTextpureText)
                first_msg = driver.execute_script("""
                    let msg = document.querySelector('[class*="TextMessageTextpureText"]');
                    return msg ? msg.textContent.trim() : '';
                """)

                if first_name not in self.today_strangers:
                    contact_time = time.strftime("%Y-%m-%d %H:%M")
                    self.today_strangers[first_name] = {
                        "first_msg": first_msg, "my_reply": self.reply_text,
                        "contact_time": contact_time
                    }

                if self.reply_text and self._send_reply(driver, self.reply_text):
                    self.log(f"已回复: {first_name}")
                    last_reply_time[first_name] = time.time()

                self._back_to_list(driver)
                time.sleep(1)

                time.sleep(self.poll)

        except Exception as e:
            self.log(f"异常: {e}")
        finally:
            if self._driver:
                try: self._driver.quit()
                except: pass
            self.status_signal.emit(self.name, "已停止")

    def _scan_reds(self, driver):
        raw = driver.execute_script("""
            let reds=[];let idx=0;
            try{
                let badges=document.querySelectorAll('span[class*="ConversationItemUnRead"]');
                badges.forEach(b=>{
                    let t=b.textContent.trim();
                    if(!t||!/^\\d+$/.test(t))return;
                    let item=b;
                    for(let d=0;d<10&&item;d++){
                        item=item.parentElement;if(!item)break;
                        if(item.className&&item.className.includes('conversationConversationItem')){
                            let name=(item.textContent||'').split(/[\\s\\n]/)[0].substring(0,15);
                            if(!name.includes('陌生人'))reds.push({name:name,unread:t});idx++;break;
                        }
                    }
                });
            }catch(e){}
            return JSON.stringify(reds);
        """)
        result = json.loads(raw) if raw else []
        return [r for r in result if isinstance(r, dict)]

    def _enter_stranger(self, driver):
        """点击 conversationStrangerBoxrowArea2→返回 True=已进入"""
        from selenium.webdriver.common.action_chains import ActionChains

        # 找可点击的陌生人入口行
        found = driver.execute_script("""
            let row = document.querySelector('[class*="conversationStrangerBoxrowArea2"]');
            if (!row) row = document.querySelector('[class*="StrangerBoxwrapper"]');
            if (row) {
                row.setAttribute('data-stranger-click', '1');
                return true;
            }
            return false;
        """)
        if not found:
            return False

        try:
            el = driver.find_element('css selector', '[data-stranger-click="1"]')
            ActionChains(driver).move_to_element(el).click().perform()
            time.sleep(4)  # 等页面切换
            return True
        except:
            return False

    def _back_to_list(self, driver):
        driver.execute_script("""
            let back=document.querySelector('[class*="back"], [class*="return"], [class*="arrow"]');
            if(back){back.closest('div,button,span').click();return;}
            let tabs=document.querySelectorAll('[class*="tab"] span, [class*="nav"] div');
            for(let t of tabs){if(/消息/.test(t.textContent)){t.click();return;}}
        """)

    def _send_reply(self, driver, text):
        found = driver.execute_script("""
            let inp = document.querySelector('[class*="zone-container"][class*="editor-kit-container"]');
            if (inp) { inp.focus(); inp.click(); return true; }
            let all=document.querySelectorAll('div[contenteditable="true"], textarea');
            for(let el of all){
                let r=el.getBoundingClientRect();
                if(r.height>20&&r.height<200&&r.top>window.innerHeight*0.35){inp=el;break;}
            }
            if(!inp)inp=document.querySelector('div[data-placeholder]')||document.querySelector('div[class*="rich-input"]');
            if(inp){inp.focus();inp.click();}
            return !!inp;
        """)
        if not found: return False
        time.sleep(0.3)
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.common.action_chains import ActionChains
        actions = ActionChains(driver)
        for ch in text: actions.send_keys(ch)
        actions.pause(0.3).send_keys(Keys.ENTER).perform()
        return True

    def _generate_report(self, filepath=None):
        """回主私信页→找每个人→读后续消息→生成CSV"""
        if not self.today_strangers or not self._driver:
            return None
        names = list(self.today_strangers.keys())
        if not names:
            return None

        driver = self._driver
        self.log(f"生成报告: {len(names)} 个陌生人")

        driver.get(CHAT_URL)
        time.sleep(3)

        follow_up = {}
        phone_numbers = {}

        for name in names:
            try:
                # 在主列表按昵称匹配（用title精准匹配）
                clean = self._clean_name(name)
                found = driver.execute_script("""
                    let items = document.querySelectorAll('[class*="conversationConversationItemwrapper"]');
                    for (let el of items) {
                        let title = el.querySelector('[class*="conversationConversationItemtitle"]');
                        if (title && (title.textContent||'').includes(arguments[0])) {
                            el.focus();
                            ['mousedown','mouseup','click'].forEach(e =>
                                el.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                            );
                            return true;
                        }
                    }
                    return false;
                """, clean)

                if found:
                    time.sleep(2)
                    # 读所有对方消息：取容器的全部文本（非单个span）
                    my_reply = self.reply_text
                    msgs = driver.execute_script("""
                        let results = [];
                        let containers = document.querySelectorAll('[class*="MessageItemTextcontainer"]');
                        containers.forEach(el => {
                            // 读容器内所有 TextMessageTextpureText 的完整文本
                            let spans = el.querySelectorAll('[class*="TextMessageTextpureText"]');
                            let full = '';
                            spans.forEach(s => { full += s.textContent; });
                            full = full.trim();
                            if (full) results.push(full);
                        });
                        return results;
                    """)
                    # 过滤掉我方自动回复的消息
                    if my_reply:
                        msgs = [m for m in msgs if m != my_reply]
                    if msgs:
                        # 第一条=打招呼，剩下的=后续回复
                        follow_up[name] = " | ".join(msgs[1:]) if len(msgs) > 1 else ""
                        # 提取手机号（从所有对方消息中）
                        all_text = " ".join(msgs)
                        phone_match = re.findall(r'1[3-9]\d{9}', all_text)
                        if phone_match:
                            phone_numbers[name] = phone_match[0]
                    else:
                        follow_up[name] = ""

                    self._back_to_list(driver)
                    time.sleep(1)
            except:
                follow_up[name] = ""

        # CSV
        if not filepath:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filepath = os.path.join(DESKTOP, f"陌生人回复记录_{self.name}_{ts}.csv")

        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["序号","陌生人昵称","联系时间","对方消息","我方回复","对方后续回复","用户手机号码"])
            for i, name in enumerate(names, 1):
                info = self.today_strangers[name]
                w.writerow([i, name, info.get("contact_time",""), info.get("first_msg",""),
                           info.get("my_reply",""), follow_up.get(name,""),
                           phone_numbers.get(name,"")])

        self.log(f"报告已保存: {os.path.basename(filepath)}")
        self.report_ready.emit(self.name, filepath)
        return filepath


# ===== GUI =====

STYLE = """
QMainWindow{background:#0d1f14}QTabWidget::pane{border:1px solid #1a3a28;background:#132818}
QTabBar::tab{background:#1a3522;color:#8DC891;padding:8px 16px;border:none}
QTabBar::tab:selected{background:#006B3F;color:#fff;font-weight:bold}
QTableWidget{background:#0f1f14;color:#ddd;gridline-color:#1a3a28;border:1px solid #1a3a28}
QHeaderView::section{background:#1a3522;color:#8DC891;border:none;padding:4px}
QLineEdit,QTextEdit{background:#172d1f;color:#ddd;border:1px solid #2a4a38;padding:6px;border-radius:4px}
QPushButton{background:#1a3522;color:#ddd;border:1px solid #2a4a38;padding:6px 14px;border-radius:4px}
QPushButton:hover{background:#234a30}
QPushButton#btnStart{background:#D4AF37;color:#000;font-weight:bold;border:none}
QPushButton#btnStart:hover{background:#e6c544}
QPushButton#btnStop{background:#555;border:none}
QPushButton#btnAdd{background:#006B3F;color:#fff;border:none}
QPushButton#btnAdd:hover{background:#008a52}
QLabel{color:#8DC891}
QGroupBox{color:#8DC891;border:1px solid #1a3a28;border-radius:6px;margin-top:8px;padding-top:12px}
QGroupBox::title{subcontrol-origin:margin;left:12px}
QStatusBar{background:#0f1f14;color:#8DC891}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("抖音私信智能助手 - 遵义农商银行 辛振宇")
        self.setGeometry(100, 100, 1050, 620)
        self.setStyleSheet(STYLE)
        self.config = load_rules()
        self.workers = {}
        self.tabs = {}
        self._build_ui()
        self._refresh_tabs()
        self.statusBar().showMessage("抖音私信智能助手 | 遵义农商银行 辛振宇 | 就绪")

    def _build_ui(self):
        c = QWidget(); self.setCentralWidget(c); ml = QVBoxLayout(c)
        # 品牌头部
        hdr = QLabel(" 抖音私信智能助手 · 遵义农商银行 辛振宇")
        hdr.setStyleSheet("background:#006B3F;color:#D4AF37;font-size:14px;font-weight:bold;padding:8px;")
        hdr.setFixedHeight(36)
        ml.addWidget(hdr)
        top = QHBoxLayout()
        b = QPushButton("+ 添加账号"); b.setObjectName("btnAdd"); b.clicked.connect(self._add_account); top.addWidget(b)
        b = QPushButton("💾 保存"); b.clicked.connect(self._save); top.addWidget(b)
        top.addStretch()
        b = QPushButton("▶ 全部启动"); b.setObjectName("btnStart"); b.clicked.connect(self._start_all); top.addWidget(b)
        b = QPushButton("⏹ 全部停止"); b.setObjectName("btnStop"); b.clicked.connect(self._stop_all); top.addWidget(b)
        b = QPushButton("📊 全部导出"); b.setStyleSheet("background:#D4AF37;color:#000;font-weight:bold;border:none;padding:6px 14px;border-radius:4px;"); b.clicked.connect(self._export_all); top.addWidget(b)
        ml.addLayout(top)
        self.tab_w = QTabWidget(); ml.addWidget(self.tab_w)
        g = QGroupBox("运行日志"); vl = QVBoxLayout(g)
        self.log_t = QTextEdit(); self.log_t.setReadOnly(True); self.log_t.setMaximumHeight(140)
        self.log_t.setFont(QFont("Consolas", 9)); vl.addWidget(self.log_t)
        ml.addWidget(g)

    def _refresh_tabs(self):
        self.tab_w.clear(); self.tabs.clear()
        for i, a in enumerate(self.config.get("accounts", [])): self._add_tab(i, a)

    def _add_tab(self, i, a):
        t = QWidget(); l = QVBoxLayout(t)
        r1 = QHBoxLayout()
        r1.addWidget(QLabel("名称:")); nm = QLineEdit(a.get("name", f"账号{i+1}")); r1.addWidget(nm)
        en = QCheckBox("启用"); en.setChecked(a.get("enabled", True)); r1.addWidget(en); r1.addStretch()
        st = QLabel("⚪ 未启动"); r1.addWidget(st); l.addLayout(r1)

        r2 = QHBoxLayout(); r2.addWidget(QLabel("回复内容:"))
        rp = QLineEdit(a.get("reply_text", "")); r2.addWidget(rp); l.addLayout(r2)

        r4 = QHBoxLayout()
        r4.addStretch()
        b = QPushButton("▶ 启动"); b.setObjectName("btnStart"); b.clicked.connect(lambda _, x=i: self._start(x)); r4.addWidget(b)
        b = QPushButton("✓ 确认已登录"); b.setStyleSheet("background:#25f4ee;color:#000;font-weight:bold;")
        b.clicked.connect(lambda _, x=i: self._confirm_login(x)); r4.addWidget(b)
        b = QPushButton("📊 导出记录"); b.setStyleSheet("background:#ff9a44;color:#000;font-weight:bold;")
        b.clicked.connect(lambda _, x=i: self._export_record(x)); r4.addWidget(b)
        b = QPushButton("⏹ 停止"); b.clicked.connect(lambda _, x=i: self._stop(x)); r4.addWidget(b)
        l.addLayout(r4)

        r5 = QHBoxLayout(); r5.addStretch(); b = QPushButton("🗑 删除账号")
        b.clicked.connect(lambda _, x=i: self._del(x)); r5.addWidget(b); l.addLayout(r5)

        self.tab_w.addTab(t, a.get("name", f"账号{i+1}"))
        self.tabs[i] = {"name": nm, "enabled": en, "status": st, "reply": rp}

    def _add_account(self):
        n = len(self.config["accounts"]) + 1
        self.config["accounts"].append({
            "name": f"账号{n}", "enabled": True,
            "reply_text": "请问您是遵义市哪个区县的户口呢？如需帮助请留下☎️"
        })
        self._refresh_tabs(); self._log("系统", f"已添加账号{n}")

    def _del(self, i):
        if QMessageBox.question(self, "确认", f"删除{self.config['accounts'][i]['name']}？") == QMessageBox.Yes:
            self._stop(i); del self.config["accounts"][i]; save_rules(self.config); self._refresh_tabs()

    def _read(self, i):
        t = self.tabs[i]
        return {
            "name": t["name"].text(), "enabled": t["enabled"].isChecked(),
            "reply_text": t["reply"].text(),
            "reply_text": t["reply"].text()
        }

    def _save(self):
        for i in range(len(self.config["accounts"])): self.config["accounts"][i] = self._read(i)
        save_rules(self.config)

    def _start(self, i):
        self._save(); a = self.config["accounts"][i]
        if not a["enabled"] or a["name"] in self.workers: return
        w = AccountWorker(a, i)
        w.log_signal.connect(self._log)
        w.status_signal.connect(lambda n, s, j=i: self._upd(j, s))
        w.report_ready.connect(self._on_report)
        w.start(); self.workers[a["name"]] = w; self.tabs[i]["status"].setText("🟡 等待登录...")

    def _stop(self, i):
        nm = self.config["accounts"][i]["name"]
        if nm in self.workers: self.workers[nm].stop(); self.workers[nm].wait(5000); del self.workers[nm]
        self.tabs[i]["status"].setText("⚪ 未启动")

    def _start_all(self):
        self._save()
        for i in range(len(self.config["accounts"])):
            if self.config["accounts"][i]["enabled"]: self._start(i)

    def _stop_all(self):
        for i in range(len(self.config["accounts"])): self._stop(i)

    def _confirm_login(self, idx):
        nm = self.config["accounts"][idx]["name"]
        if nm in self.workers:
            self.workers[nm].confirm_login()
            self.tabs[idx]["status"].setText("🟢 监控中")
            self.tabs[idx]["status"].setStyleSheet("color:#25f4ee;")
            self._log("系统", "确认登录，开始监控")

    def _export_record(self, idx):
        nm = self.config["accounts"][idx]["name"]
        if nm not in self.workers:
            return
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"陌生人回复记录_{nm}_{ts}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "保存导出记录", default_name, "CSV文件(*.csv)")
        if path:
            self.workers[nm].trigger_export(path)
            self._log(nm, "已触发导出...")

    def _export_all(self):
        """全部导出: 每个账号一张sheet，保存为一个xlsx"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path, _ = QFileDialog.getSaveFileName(self, "全部导出",
            f"抖音私信记录_{ts}.xlsx", "Excel文件(*.xlsx)")
        if not path: return

        import tempfile, csv as csv_mod
        tmpdir = tempfile.mkdtemp()
        files = []

        for i, a in enumerate(self.config.get("accounts", [])):
            nm = a["name"]
            if nm not in self.workers:
                continue
            csv_path = os.path.join(tmpdir, f"{nm}.csv")
            self.workers[nm].trigger_export(csv_path)
            self._log(nm, "导出中...")
            # 等待该账号导出完成（最多60秒）
            self.workers[nm]._export_done.wait(60)
            if os.path.exists(csv_path):
                files.append((nm, csv_path))

        if not files:
            QMessageBox.warning(self, "提示", "没有导出任何记录")
            return

        # 合并为xlsx
        try:
            from openpyxl import Workbook
            wb = Workbook()
            wb.remove(wb.active)  # 删除默认sheet
            for nm, fp in files:
                with open(fp, "r", encoding="utf-8-sig") as f:
                    rows = list(csv_mod.reader(f))
                if not rows: continue
                ws = wb.create_sheet(title=nm[:31])  # sheet名最长31字符
                for row in rows:
                    ws.append(row)
                # 设置列宽
                ws.column_dimensions['C'].width = 30  # 对方消息
                ws.column_dimensions['E'].width = 30  # 对方回复
            wb.save(path)
            self._log("系统", f"全部导出完成: {len(files)}个账号 → {os.path.basename(path)}")
            QMessageBox.information(self, "导出完成", f"已导出 {len(files)} 个账号到\n{path}")
        except ImportError:
            self._log("系统", "缺少openpyxl,无法生成xlsx")
            QMessageBox.warning(self, "提示", "缺少openpyxl库,请安装: pip install openpyxl")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _upd(self, i, s):
        colors = {"监控中": "#25f4ee", "等待登录": "#ff9a44", "已停止": "#aaa"}
        self.tabs[i]["status"].setText(f"● {s}")
        self.tabs[i]["status"].setStyleSheet(f"color:{colors.get(s, '#aaa')};")

    def _log(self, name, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_t.append(f"[{ts}] [{name}] {msg}")

    def _on_report(self, name, filepath):
        self._log(name, f"📁 报告: {os.path.basename(filepath)}")

    def closeEvent(self, e):
        if self.workers:
            reply = QMessageBox.question(self, "退出确认",
                "关闭前请先点击「导出记录」保存陌生人的聊天记录。\n\n确定退出？",
                QMessageBox.Yes | QMessageBox.No)
            if reply == QMessageBox.No:
                e.ignore(); return
        self._stop_all(); self._save(); e.accept()


if __name__ == "__main__":
    app = QApplication(sys.argv); app.setStyle("Fusion"); MainWindow().show(); sys.exit(app.exec_())
