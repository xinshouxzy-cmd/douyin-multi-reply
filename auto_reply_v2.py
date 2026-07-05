# -*- coding: utf-8 -*-
"""
抖音多账号私信自动回复 v4 — chat?isPopup=1 精准版本
=====================================================
基于实测验证的 DOM 结构：
  红点: SPAN[class*="ConversationItemUnRead"]
  点击: DIV[class*="conversationConversationItem"] focus+mousedown+mouseup+click
  页面: https://www.douyin.com/chat?isPopup=1
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
LOG_FILE = os.path.join(BASE_DIR, "reply_log.csv")
CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
POLL = 5

os.makedirs(PROFILES_DIR, exist_ok=True)


def load_rules():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"accounts": [
        {"name": "账号1", "enabled": True,
         "rules": [{"keyword": "在吗", "reply": "在的，有什么可以帮您？"},
                    {"keyword": "利率", "reply": "您好！利率请拨打96688咨询~"},
                    {"keyword": "贷款", "reply": "您好！贷款请到网点或拨打96688咨询~"}],
         "phone_reply": "好的，已收到您的手机号，稍后联系您~",
         "default_reply": "您好！感谢关注遵义农商银行，请拨打96688或到网点咨询~",
         "poll_interval": 5}
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

    def __init__(self, acc, idx):
        super().__init__()
        self.acc = acc
        self.idx = idx
        self.name = acc["name"]
        self.rules = acc.get("rules", [])
        self.phone_reply = acc.get("phone_reply", "")
        self.default_reply = acc.get("default_reply", "")
        self.poll = acc.get("poll_interval", POLL)
        self._stop = False
        self._login_ok = threading.Event()

    def confirm_login(self):
        self._login_ok.set()

    def stop(self):
        self._stop = True

    def log(self, msg):
        self.log_signal.emit(self.name, msg)

    def match_reply(self, text):
        if self.phone_reply and re.search(r'1[3-9]\d{9}', text):
            return self.phone_reply, "手机号"
        for r in self.rules:
            kw = r.get("keyword", "")
            if kw and kw in text:
                return r["reply"], f"关键词[{kw}]"
        if self.default_reply:
            return self.default_reply, "默认"
        return None, None

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

        driver.execute_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        driver.set_window_size(500, 800)

        try:
            driver.get("https://www.douyin.com")
            self.log("请扫码登录，完成后点「确认已登录」")
            self.status_signal.emit(self.name, "等待登录")

            while not self._login_ok.is_set() and not self._stop:
                time.sleep(0.5)
            if self._stop: return

            # 跳转到纯聊天页
            self.log("跳转到聊天页...")
            driver.get(CHAT_URL)
            time.sleep(3)
            self.status_signal.emit(self.name, "监控中")
            self.log("✅ 开始监控")

            seen_counts = {}

            while not self._stop:
                reds = self._scan_reds(driver)

                for red in reds:
                    if self._stop: break
                    cid = red.get("id")
                    count = int(red.get("unread", "1")) if red.get("unread","1").isdigit() else 1
                    if cid in seen_counts and count <= seen_counts[cid]:
                        continue

                    name = red.get("name", "用户")
                    self.log(f"点击红点: {name}({count}条)")

                    # 点击对话 — 已验证成功的 focus > mousedown > mouseup > click 事件链
                    ok = driver.execute_script("""
                        let badges = document.querySelectorAll('span[class*="ConversationItemUnRead"]');
                        for (let b of badges) {
                            let t = b.textContent.trim();
                            if (!t || !/^\\d+$/.test(t)) continue;
                            let item = b;
                            for (let d=0; d<10 && item; d++) {
                                item = item.parentElement;
                                if (!item) break;
                                if (item.className && item.className.includes('conversationConversationItem')) {
                                    item.focus();
                                    ['mousedown','mouseup','click'].forEach(e =>
                                        item.dispatchEvent(new MouseEvent(e,{bubbles:true,cancelable:true}))
                                    );
                                    return true;
                                }
                            }
                        }
                        return false;
                    """)
                    if not ok:
                        continue
                    time.sleep(2)

                    # 确认红点消失 + 输入框出现
                    badge_gone = driver.execute_script("""
                        let b = document.querySelector('span[class*="ConversationItemUnRead"]');
                        let inp = document.querySelector('div[contenteditable="true"], textarea');
                        return !b && !!inp;
                    """)
                    if not badge_gone:
                        self._back_to_list(driver)
                        continue
                    time.sleep(1)

                    # 读消息
                    last_msg = driver.execute_script("""
                        let all = document.querySelectorAll('div[class*="message-content"], div[class*="bubble"], div[class*="msg-text"], span[class*="content"]');
                        for (let i=all.length-1; i>=0; i--) {
                            let t = all[i].textContent.trim();
                            if (t.length>0 && t.length<500 && !t.includes('发送')) return t;
                        }
                        return '';
                    """)

                    reply_text, rule = self.match_reply(last_msg)
                    if reply_text and self._send_reply(driver, reply_text):
                        self.log(f"📤 回复{name}: {reply_text[:30]} [{rule}]")
                        self._write_log(name, last_msg, reply_text)
                        if cid in seen_counts: del seen_counts[cid]
                        seen_counts[cid] = count
                        time.sleep(1)

                    self._back_to_list(driver)
                    time.sleep(1)

                time.sleep(self.poll)

        except Exception as e:
            self.log(f"异常: {e}")
        finally:
            try: driver.quit()
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
                            let name=(item.textContent||'').split(/[\\s\\n]/)[0].substring(0,12);
                            reds.push({id:'c_'+idx,index:idx,name:name,unread:t});idx++;break;
                        }
                    }
                });
            }catch(e){}
            return JSON.stringify(reds);
        """)
        result = json.loads(raw) if raw else []
        cleaned = [r for r in result if isinstance(r, dict) and 'id' in r]
        if not hasattr(self, '_scan_count'): self._scan_count = 0
        self._scan_count += 1
        if self._scan_count % 5 == 1 and cleaned:
            names = [r.get('name','?')[:8] for r in cleaned[:5]]
            self.log(f"红点: {', '.join(names)}")
        return cleaned

    def _back_to_list(self, driver):
        driver.execute_script("""
            let back=document.querySelector('[class*="back"], [class*="return"], [class*="arrow"]');
            if(back){back.closest('div,button,span').click();return;}
            let tabs=document.querySelectorAll('[class*="tab"] span, [class*="nav"] div');
            for(let t of tabs){if(/消息/.test(t.textContent)){t.click();return;}}
        """)

    def _send_reply(self, driver, text):
        found = driver.execute_script("""
            let inp=null;
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

    def _write_log(self, sender, msg_in, msg_out):
        try:
            import csv
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            existed = os.path.exists(LOG_FILE)
            with open(LOG_FILE, "a", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                if not existed: w.writerow(["时间","账号","联系人","收到消息","回复内容"])
                w.writerow([ts, self.name, sender, msg_in, msg_out])
        except: pass


# ===== GUI =====

STYLE = """
QMainWindow{background:#1e1e1e}QTabWidget::pane{border:1px solid #333;background:#252525}
QTabBar::tab{background:#2d2d2d;color:#aaa;padding:8px 16px;border:none}
QTabBar::tab:selected{background:#c41230;color:#fff}
QTableWidget{background:#1a1a1a;color:#ddd;gridline-color:#333;border:1px solid #333}
QHeaderView::section{background:#2d2d2d;color:#aaa;border:none;padding:4px}
QLineEdit,QTextEdit{background:#2d2d2d;color:#ddd;border:1px solid #444;padding:4px;border-radius:4px}
QPushButton{background:#3a3a3a;color:#ddd;border:none;padding:6px 14px;border-radius:4px}
QPushButton:hover{background:#4a4a4a}
QPushButton#btnStart{background:#c41230;color:#fff;font-weight:bold}
QPushButton#btnStart:hover{background:#e01438}
QPushButton#btnStop{background:#555}
QPushButton#btnAdd{background:#25f4ee;color:#000}
QLabel{color:#bbb}
QGroupBox{color:#aaa;border:1px solid #333;border-radius:6px;margin-top:8px;padding-top:12px}
QGroupBox::title{subcontrol-origin:margin;left:12px}
QStatusBar{background:#2d2d2d;color:#aaa}
"""


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("抖音多账号私信自动回复 - 遵义农商银行")
        self.setGeometry(100,100,1050,720)
        self.setStyleSheet(STYLE)
        self.config = load_rules()
        self.workers = {}
        self.tabs = {}
        self._build_ui()
        self._refresh_tabs()
        self.statusBar().showMessage("就绪")

    def _build_ui(self):
        c=QWidget();self.setCentralWidget(c);ml=QVBoxLayout(c)
        top=QHBoxLayout()
        b=QPushButton("+ 添加账号");b.setObjectName("btnAdd");b.clicked.connect(self._add_account);top.addWidget(b)
        b=QPushButton("💾 保存");b.clicked.connect(self._save);top.addWidget(b)
        top.addStretch()
        b=QPushButton("▶ 全部启动");b.setObjectName("btnStart");b.clicked.connect(self._start_all);top.addWidget(b)
        b=QPushButton("⏹ 全部停止");b.setObjectName("btnStop");b.clicked.connect(self._stop_all);top.addWidget(b)
        ml.addLayout(top)
        self.tab_w=QTabWidget();ml.addWidget(self.tab_w)
        g=QGroupBox("运行日志");vl=QVBoxLayout(g)
        btns=QHBoxLayout()
        b=QPushButton("导出CSV");b.clicked.connect(self._export_log);btns.addWidget(b)
        btns.addStretch();vl.addLayout(btns)
        self.log_t=QTextEdit();self.log_t.setReadOnly(True);self.log_t.setMaximumHeight(140)
        self.log_t.setFont(QFont("Consolas",9));vl.addWidget(self.log_t)
        ml.addWidget(g)

    def _refresh_tabs(self):
        self.tab_w.clear();self.tabs.clear()
        for i,a in enumerate(self.config.get("accounts",[])):self._add_tab(i,a)

    def _add_tab(self,i,a):
        t=QWidget();l=QVBoxLayout(t)
        r1=QHBoxLayout()
        r1.addWidget(QLabel("名称:"));nm=QLineEdit(a.get("name",f"账号{i+1}"));r1.addWidget(nm)
        en=QCheckBox("启用");en.setChecked(a.get("enabled",True));r1.addWidget(en);r1.addStretch()
        st=QLabel("⚪ 未启动");r1.addWidget(st);l.addLayout(r1)
        tb=QTableWidget();tb.setColumnCount(2)
        tb.setHorizontalHeaderLabels(["关键词","回复内容"])
        tb.horizontalHeader().setSectionResizeMode(0,QHeaderView.Interactive)
        tb.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        tb.setColumnWidth(0,120)
        for j,r in enumerate(a.get("rules",[])):
            if j>=tb.rowCount():tb.insertRow(j)
            tb.setItem(j,0,QTableWidgetItem(r.get("keyword","")))
            tb.setItem(j,1,QTableWidgetItem(r.get("reply","")))
        if tb.rowCount()==0:tb.insertRow(0)
        l.addWidget(QLabel("关键词规则:"));l.addWidget(tb)
        rb=QHBoxLayout()
        b=QPushButton("+ 添加");b.clicked.connect(lambda:tb.insertRow(tb.rowCount()));rb.addWidget(b)
        b=QPushButton("- 删除");b.clicked.connect(lambda:(tb.currentRow()>=0 and tb.rowCount()>1)and tb.removeRow(tb.currentRow()));rb.addWidget(b)
        rb.addStretch();l.addLayout(rb)
        r2=QHBoxLayout();r2.addWidget(QLabel("手机号回复:"));ph=QLineEdit(a.get("phone_reply",""));r2.addWidget(ph);l.addLayout(r2)
        r3=QHBoxLayout();r3.addWidget(QLabel("默认回复:"));df=QLineEdit(a.get("default_reply",""));r3.addWidget(df);l.addLayout(r3)
        r4=QHBoxLayout();r4.addWidget(QLabel("间隔(秒):"));pi=QLineEdit(str(a.get("poll_interval",5)));pi.setMaximumWidth(50);r4.addWidget(pi);r4.addStretch()
        b=QPushButton("▶ 启动");b.setObjectName("btnStart");b.clicked.connect(lambda _,x=i:self._start(x));r4.addWidget(b)
        b=QPushButton("✓ 确认已登录");b.setStyleSheet("background:#25f4ee;color:#000;font-weight:bold;");b.clicked.connect(lambda _,x=i:self._confirm_login(x));r4.addWidget(b)
        b=QPushButton("⏹ 停止");b.clicked.connect(lambda _,x=i:self._stop(x));r4.addWidget(b);l.addLayout(r4)
        r5=QHBoxLayout();r5.addStretch();b=QPushButton("🗑 删除账号");b.clicked.connect(lambda _,x=i:self._del(x));r5.addWidget(b);l.addLayout(r5)
        self.tab_w.addTab(t,a.get("name",f"账号{i+1}"))
        self.tabs[i]={"name":nm,"enabled":en,"status":st,"table":tb,"phone":ph,"default":df,"poll":pi}

    def _add_account(self):
        n=len(self.config["accounts"])+1
        self.config["accounts"].append({"name":f"账号{n}","enabled":True,"rules":[{"keyword":"在吗","reply":"在的！"}],"phone_reply":"好的收到~","default_reply":"您好！感谢关注遵义农商银行！","poll_interval":5})
        self._refresh_tabs();self._log("系统",f"已添加账号{n}")

    def _del(self,i):
        if QMessageBox.question(self,"确认",f"删除{self.config['accounts'][i]['name']}？")==QMessageBox.Yes:
            self._stop(i);del self.config["accounts"][i];save_rules(self.config);self._refresh_tabs()

    def _read(self,i):
        t=self.tabs[i];tb=t["table"];rules=[]
        for r in range(tb.rowCount()):
            kw=tb.item(r,0);rp=tb.item(r,1)
            if kw and kw.text().strip():rules.append({"keyword":kw.text().strip(),"reply":(rp.text()if rp else"")})
        return{"name":t["name"].text(),"enabled":t["enabled"].isChecked(),"rules":rules,"phone_reply":t["phone"].text(),"default_reply":t["default"].text(),"poll_interval":int(t["poll"].text())if t["poll"].text().isdigit()else 5}

    def _save(self):
        for i in range(len(self.config["accounts"])):self.config["accounts"][i]=self._read(i)
        save_rules(self.config)

    def _start(self,i):
        self._save();a=self.config["accounts"][i]
        if not a["enabled"] or a["name"] in self.workers:return
        w=AccountWorker(a,i);w.log_signal.connect(self._log);w.status_signal.connect(lambda n,s,j=i:self._upd(j,s))
        w.start();self.workers[a["name"]]=w;self.tabs[i]["status"].setText("🟡 等待登录...")

    def _stop(self,i):
        nm=self.config["accounts"][i]["name"]
        if nm in self.workers:self.workers[nm].stop();self.workers[nm].wait(5000);del self.workers[nm]
        self.tabs[i]["status"].setText("⚪ 未启动")

    def _start_all(self):
        self._save()
        for i in range(len(self.config["accounts"])):self._start(i)if self.config["accounts"][i]["enabled"]else None

    def _stop_all(self):
        for i in range(len(self.config["accounts"])):self._stop(i)

    def _confirm_login(self,idx):
        nm=self.config["accounts"][idx]["name"]
        if nm in self.workers:self.workers[nm].confirm_login();self.tabs[idx]["status"].setText("🟢 监控中");self.tabs[idx]["status"].setStyleSheet("color:#25f4ee;");self._log("系统","确认登录，开始监控")

    def _upd(self,i,s):
        colors={"监控中":"#25f4ee","等待登录":"#ff9a44","已停止":"#aaa"}
        self.tabs[i]["status"].setText(f"● {s}");self.tabs[i]["status"].setStyleSheet(f"color:{colors.get(s,'#aaa')};")

    def _log(self,name,msg):
        ts=datetime.now().strftime("%H:%M:%S");self.log_t.append(f"[{ts}] [{name}] {msg}")

    def _export_log(self):
        if not os.path.exists(LOG_FILE):QMessageBox.information(self,"提示","暂无日志");return
        p,_=QFileDialog.getSaveFileName(self,"导出日志","回复记录.csv","CSV(*.csv)")
        if p:import shutil;shutil.copy(LOG_FILE,p);self._log("系统",f"已导出: {p}")

    def closeEvent(self,e):
        self._stop_all();self._save();e.accept()


if __name__=="__main__":
    app=QApplication(sys.argv);app.setStyle("Fusion");MainWindow().show();sys.exit(app.exec_())
