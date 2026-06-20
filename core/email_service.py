import smtplib
import os
import logging
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage # Added for embedding images
from datetime import datetime, timedelta
from dotenv import load_dotenv
from core.database import get_config, get_all_states_db, get_detailed_trade_history_db, get_canceled_orders_db
from core.utils import format_symbol_display

import matplotlib.pyplot as plt # Added for plotting
import matplotlib
matplotlib.use('Agg') # Use non-interactive backend for server environments
import io # Added for BytesIO
from email.message import Message # Import Message base class for type hinting
logger = logging.getLogger(__name__)

# 공통 환경 변수 로드
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, "env", ".env"))

def send_email(subject: str, content_or_msg_object: str | MIMEMultipart):
    """Zoho SMTP를 이용한 이메일 발송 공통 함수"""
    smtp_host = os.getenv("ZOHO_SMTP_SERVER")
    smtp_port = int(os.getenv("ZOHO_SMTP_PORT", "465"))
    smtp_user = os.getenv("ZOHO_SMTP_USER")
    smtp_pass = os.getenv("ZOHO_SMTP_PASSWORD")
    sender = os.getenv("ZOHO_SMTP_USER") # 보통 발신인은 계정 이메일과 동일
    receiver = os.getenv("ZOHO_RECEIVER_EMAIL") or smtp_user

    if not all([smtp_user, smtp_pass, receiver]):
        logger.error("[Email] SMTP 설정이 누락되었습니다.")
        return False

    if isinstance(content_or_msg_object, MIMEMultipart):
        # If it's already a MIMEMultipart object (e.g., for monthly reports with images)
        msg = content_or_msg_object
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = receiver
    else:
        # If it's a simple HTML string (e.g., for daily reports)
        msg = MIMEMultipart()
        msg['Subject'] = subject
        msg['From'] = sender
        msg['To'] = receiver
        msg.attach(MIMEText(content_or_msg_object, 'html', 'utf-8')) # Explicitly set charset

    try:
        # Zoho SSL 465 포트 사용
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(sender, receiver, msg.as_string())
        logger.info(f"[Email] 이메일 발송 성공: {subject}")
        return True
    except Exception as e:
        logger.error(f"[Email] 이메일 발송 실패: {e}")
        return False

def generate_daily_report_html(market):
    """시장별 일일 현황 HTML 생성"""
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    states = get_all_states_db(market=market)
    currency = "₩" if market == "KR" else "$"
    
    html = f"""
    <html>
    <body style="font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #333; line-height: 1.6; padding: 20px; max-width: 800px; margin: auto; background-color: #ffffff;">
        <h2 style="color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; margin-top: 0;">🚀 {market} 시장 일일 운용 보고서 ({today_str})</h2>
        
        <h3 style="color: #34495e; margin-top: 30px; margin-bottom: 15px;">📊 전략별 현재 상태</h3>
        <table style="border-collapse: collapse; width: 100%; text-align: left; border: 1px solid #e9ecef; border-radius: 8px; overflow: hidden; margin-bottom: 25px;">
            <tr style="background-color: #f8f9fa;">
                <th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">전략(별칭)</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">종목</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">회차/목표V</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">평단/Pool</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">보유수량</th>
            </tr>
    """
    for s in states:
        stype = s.get('strategy_type')
        alias = s.get('strategy_name')
        sym = s.get('symbol')
        if stype == "CA":
            val1 = f"{s.get('current_turn', 0):.1f}T"
            avg_price = s.get('avg_price', 0)
            val2 = f"{currency}{int(avg_price):,}" if market == "KR" else f"{currency}{avg_price:,.2f}"
        else:
            val1 = f"{currency}{int(s.get('cycle_V', 0)):,}" if market == "KR" else f"{currency}{s.get('cycle_V', 0):,.0f}"
            val2 = f"{currency}{int(s.get('pool', 0)):,}" if market == "KR" else f"{currency}{s.get('pool', 0):,.0f}"
        
        html += f"<tr><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{stype}({alias})</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{format_symbol_display(sym, market)}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{val1}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{val2}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{s.get('total_shares', 0)}</td></tr>"
    
    html += "</table>"
    
    # [신규] 미국 시장일 경우 환율 요약 정보 추가
    if market == "US":
        usd_krw = get_config("USDKRW", "0.00")
        usd_diff = get_config("USDKRW_DIFF", "0.00")
        usd_pct = get_config("USDKRW_PCT", "0.00")
        html += f"<div style='background-color: #f8f9fa; padding: 15px; border-radius: 10px; border-left: 5px solid #3498db; margin: 25px 0;'>"
        html += f"<span style='color: #2c3e50; font-weight: bold;'>💵 실시간 환율 정보:</span> 1$ = <b>₩{usd_krw}</b> <span style='font-size: 0.9em; color: #666;'>(전일대비 {float(usd_diff):+.2f}원, {float(usd_pct):+.2f}%)</span></div>"

    html += "<h3 style='color: #34495e; margin-top: 30px;'>💰 금일 실현 손익 내역</h3>"
    
    # [수정] 거래 내역 필터링 로직 개선
    cols, rows = get_detailed_trade_history_db(None, market=market)
    
    # DB의 date 컬럼(r[0])이 시각을 포함(YYYY-MM-DD HH:MM:SS)하므로 startswith으로 비교
    # US 시장의 경우 KST 서버에서 실행 시 날짜가 이미 다음 날로 넘어갔을 수 있음 (토요일 새벽)
    today_trades = [r for r in rows if str(r[0]).startswith(today_str) and r[5] == "SELL"]
    
    # 만약 오늘 내역이 하나도 없다면, US 시장의 경우 전일(금요일) 내역을 한 번 더 확인 (KST 시차 대응)
    if not today_trades and market == "US":
        yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        today_trades = [r for r in rows if str(r[0]).startswith(yesterday_str) and r[5] == "SELL"]
        if today_trades:
            html = html.replace(f"({today_str})", f"({yesterday_str})")
    
    if not today_trades:
        html += "<p>오늘 매도 내역이 없습니다.</p>"
    else:
        html += """
        <table style="border-collapse: collapse; width: 100%; text-align: left; border: 1px solid #e9ecef; border-radius: 8px; overflow: hidden;">
            <tr style="background-color: #f8f9fa;">
                <th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">종목</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">별칭</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">이익</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">수익률</th>
            </tr>
        """
        total_profit = 0
        for r in today_trades:
            profit = r[14] # realized_profit
            total_profit += profit
            profit_fmt = f"{int(profit):,}" if market == "KR" else f"{profit:,.2f}"
            html += f"<tr><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{format_symbol_display(r[1], market)}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5;'>{r[4]}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5; color: #e74c3c; font-weight: bold;'>{currency}{profit_fmt}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f1f3f5; color: #e74c3c; font-weight: bold;'>{r[15]:.2f}%</td></tr>"
        total_profit_fmt = f"{int(total_profit):,}" if market == "KR" else f"{total_profit:,.2f}"
        html += f"</table><p style='text-align: right; font-size: 1.1em;'><b>오늘의 총 수익: <span style='color: #e74c3c;'>{currency}{total_profit_fmt}</span></b></p>"

    # [수정] 주간/월간 실현 손익 요약 (로컬 DB 데이터 기반 계산으로 변경)
    # 이유: API 호출은 정산 지연 등으로 당일 내역이 누락될 수 있으나, DB는 웹소켓 등을 통해 최신 체결 정보를 보유함
    try:
        # 이번 주 월요일
        p_start_week = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
        # 이번 달 1일
        p_start_month = now.replace(day=1).strftime("%Y-%m-%d")
        
        # DB rows에서 기간별 합계 직접 산출 (r[14]: realized_profit, r[5]: side)
        # r[13]: avg_price, r[8]: qty
        week_data = [r for r in rows if str(r[0]) >= p_start_week and r[5] == "SELL"]
        month_data = [r for r in rows if str(r[0]) >= p_start_month and r[5] == "SELL"]

        def calc_summary(trades):
            profit = sum(float(r[14] or 0) for r in trades)
            cost = sum(float(r[13] or 0) * float(r[8] or 0) for r in trades)
            rate = (profit / cost * 100) if cost > 0 else 0
            return profit, rate

        p_week, r_week = calc_summary(week_data)
        p_month, r_month = calc_summary(month_data)
        
        disp_start_week = p_start_week.replace('-', '')
        disp_start_month = p_start_month.replace('-', '')
        disp_today = today_str.replace('-', '')

        html += f"""
        <br><h3 style="color: #34495e;">📈 {market} 시장 실현 손익 요약</h3>
        <table style="border-collapse: collapse; width: 100%; text-align: left; border: 1px solid #e9ecef;">
            <tr style="background-color: #f8f9fa;">
                <th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">구분</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">조회 기간</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">실현 손익</th><th style="padding: 12px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">수익률</th>
            </tr>
            <tr><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5;">주간 (이번 주)</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5;">{disp_start_week} ~ {disp_today}</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5; font-weight: bold;">{currency}{format_currency_simple(p_week, market)}</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5; color: {'#e74c3c' if p_week >= 0 else '#3498db'}; font-weight: bold;">{r_week:+.2f}%</td></tr>
            <tr><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5;">월간 (이번 달)</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5;">{disp_start_month} ~ {disp_today}</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5; font-weight: bold;">{currency}{format_currency_simple(p_month, market)}</td><td style="padding: 10px 15px; border-bottom: 1px solid #f1f3f5; color: {'#e74c3c' if p_month >= 0 else '#3498db'}; font-weight: bold;">{r_month:+.2f}%</td></tr>
        </table>
        """
    except Exception as e:
        logger.error(f"[Email] 기간별 손익 요약 생성 실패: {e}")

    # [신규] 금일 미체결 취소 내역 추가
    c_cols, c_rows = get_canceled_orders_db(market)
    if c_rows:
        html += "<br><h3 style='color: #e74c3c; margin-top: 30px;'>🚫 금일 미체결 취소 내역</h3>"
        html += """
        <table style="border-collapse: collapse; width: 100%; text-align: left; border: 1px solid #f8d7da; border-radius: 8px; overflow: hidden;">
            <tr style="background-color: #f8d7da;">
                <th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">종목</th><th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">별칭</th><th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">구분</th><th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">가격</th><th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">수량</th><th style="padding: 12px 15px; color: #721c24; border-bottom: 2px solid #f5c6cb;">시간</th>
            </tr>
        """
        for r in c_rows:
            # r: [symbol, strategy_name, side, price, qty, timestamp]
            price_val = r[3]
            price_fmt = f"{int(price_val):,}" if market == "KR" else f"{price_val:,.2f}"
            # 시간 값에서 시:분:초만 추출 (YYYY-MM-DD HH:MM:SS -> HH:MM:SS)
            time_only = r[5].split(' ')[-1] if ' ' in r[5] else r[5]
            
            html += f"<tr><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{format_symbol_display(r[0], market)}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{r[1]}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{r[2]}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{currency}{price_fmt}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{int(r[4])}</td><td style='padding: 10px 15px; border-bottom: 1px solid #f5c6cb; color: #721c24;'>{time_only}</td></tr>"
        html += "</table>"
        html += "<p style='font-size: 11px; color: #721c24; background-color: #f8d7da; padding: 8px; border-radius: 4px; margin-top: 5px;'>* 주가가 LOC/지정가 조건에 도달하지 않아 체결되지 않고 자동 취소된 주문입니다.</p>"

    html += """
        <br><br>
        <p style="font-size: 12px; color: #888;">본 메일은 CAVR 시스템에서 자동 발송되었습니다.</p>
    </body>
    </html>
    """
    return html

def job_send_daily_report(market):
    """스케줄러에서 호출할 작업"""
    subject = f"🔔 [CAVR] {market} 시장 장 종료 보고서"
    content = generate_daily_report_html(market)
    send_email(subject, content)

def _generate_monthly_profit_graph(market: str, start_date: datetime, end_date: datetime) -> Optional[io.BytesIO]:
    """
    지정된 시장과 기간에 대한 월간 누적 실현 손익 그래프를 생성합니다.
    """
    try:
        cols, rows = get_detailed_trade_history_db(None, market=market)
        if not rows:
            logger.info(f"[Email] {market} 시장의 거래 내역이 없어 그래프를 생성할 수 없습니다.")
            return None

        import pandas as pd # Import pandas locally to avoid global dependency if not needed elsewhere
        df = pd.DataFrame(rows, columns=cols)
        df['date'] = pd.to_datetime(df['date'], format='mixed')
        
        # 지난 달 데이터만 필터링
        df_filtered = df[(df['date'] >= start_date) & (df['date'] <= end_date)]
        df_sells = df_filtered[df_filtered['side'] == 'SELL'].copy()
        
        if df_sells.empty:
            logger.info(f"[Email] {market} 시장의 지난 달 매도 내역이 없어 그래프를 생성할 수 없습니다.")
            return None

        # 월별로 그룹화하여 실현 손익 합산 및 누적 계산
        df_sells.set_index('date', inplace=True)
        # 'MS' for Month Start, 'M' for Month End
        monthly_profit = df_sells['realized_profit'].resample('MS').sum() 
        cumulative_profit = monthly_profit.cumsum()

        if cumulative_profit.empty:
            logger.info(f"[Email] {market} 시장의 지난 달 누적 손익 데이터가 없어 그래프를 생성할 수 없습니다.")
            return None

        # 그래프 생성
        fig, ax = plt.subplots(figsize=(10, 5))
        cumulative_profit.plot(kind='line', marker='o', ax=ax)
        
        ax.set_title(f'{market} 시장 월간 누적 실현 손익 ({start_date.strftime("%Y.%m")} ~ {end_date.strftime("%Y.%m")})', fontsize=14)
        ax.set_xlabel('월', fontsize=12)
        ax.set_ylabel(f'누적 실현 손익 ({format_currency_simple(1, market)[0]})', fontsize=12) # Get currency symbol
        ax.grid(True, linestyle='--', alpha=0.6)
        ax.ticklabel_format(style='plain', axis='y') # Disable scientific notation for y-axis
        
        # Y축 포맷팅 (KR은 정수, US는 소수점 2자리)
        if market == "KR":
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{int(x):,}'))
        else:
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, p: f'{x:,.2f}'))

        plt.tight_layout()

        # 이미지를 BytesIO 객체에 저장
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=150)
        buf.seek(0)
        plt.close(fig) # Close the figure to free memory
        return buf

    except Exception as e:
        logger.error(f"[Email] {market} 월간 손익 그래프 생성 실패: {e}", exc_info=True)
        return None

def generate_monthly_report_html() -> MIMEMultipart:
    """지난 달 전체 실현 손익 요약 리포트 생성"""
    now = datetime.now()
    # 지난 달 계산
    # ... (omitted for brevity, no changes here) ...

    first_day_this_month = now.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    first_day_prev_month = last_day_prev_month.replace(day=1)
    
    start_dt = first_day_prev_month.strftime("%Y%m%d")
    end_dt = last_day_prev_month.strftime("%Y%m%d")
    month_str = first_day_prev_month.strftime("%Y년 %m월")
    
    # MIMEMultipart 객체 생성 (이미지 첨부를 위해)
    msg_root = MIMEMultipart('related')
    msg_root['MIME-Version'] = '1.0' # Explicitly set MIME-Version for the root message

    html_body_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif;">
        <h2 style="color: #2e6c80;">📅 [CAVR] 월간 투자 결산 리포트 ({month_str})</h2>
        <p>조회 기간: {first_day_prev_month.strftime('%Y-%m-%d')} ~ {last_day_prev_month.strftime('%Y-%m-%d')}</p>
        <hr>
    """
    # 각 시장별로 수익 조회
    for market in ["KR", "US"]:
        currency = "₩" if market == "KR" else "$"
        try:
            from core.brokers.toss import TossBroker
            broker = TossBroker(market=market)
            
            total_profit = broker.get_period_profit(start_dt, end_dt)
            
            # DB에서도 내역 가져오기 (종목별 상세를 위해)
            cols, rows = get_detailed_trade_history_db(None, market=market)
            # 기간 필터링
            p_start = first_day_prev_month.strftime("%Y-%m-%d")
            p_end = last_day_prev_month.strftime("%Y-%m-%d")
            month_trades = [r for r in rows if p_start <= r[0] <= p_end + " 23:59:59" and r[5] == "SELL"]
            
            html_body_content += f"<h3>🌍 {market} 시장 수익 요약</h3>"
            html_body_content += f"<p><b>총 실현 손익: {currency}{format_currency_simple(total_profit, market)}</b></p>"
            
            if month_trades:
                html += """
                <table style="border-collapse: collapse; width: 100%; text-align: left; font-size: 13px; border: 1px solid #e9ecef; border-radius: 8px; overflow: hidden;">
                    <tr style="background-color: #f8f9fa;">
                        <th style="padding: 10px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">종목</th><th style="padding: 10px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">별칭</th><th style="padding: 10px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">이익</th><th style="padding: 10px 15px; border-bottom: 2px solid #dee2e6; color: #495057;">수익률</th>
                    </tr>
                """
                summary = {}
                for r in month_trades:
                    sym = r[1]
                    alias = r[4] or "기타"
                    key = (sym, alias)
                    if key not in summary:
                        summary[key] = {'profit': 0.0, 'rate_sum': 0.0, 'count': 0}
                    summary[key]['profit'] += float(r[14] or 0)
                    summary[key]['rate_sum'] += float(r[15] or 0)
                    summary[key]['count'] += 1
                
                for (sym, alias), data in summary.items():
                    avg_rate = data['rate_sum'] / data['count'] if data['count'] > 0 else 0.0
                    html_body_content += f"<tr><td style='padding: 8px 15px; border-bottom: 1px solid #f1f3f5;'>{format_symbol_display(sym, market)}</td><td style='padding: 8px 15px; border-bottom: 1px solid #f1f3f5;'>{alias}</td><td style='padding: 8px 15px; border-bottom: 1px solid #f1f3f5; color: {'#e74c3c' if data['profit'] >= 0 else '#3498db'}; font-weight: bold;'>{currency}{format_currency_simple(data['profit'], market)}</td><td style='padding: 8px 15px; border-bottom: 1px solid #f1f3f5; color: {'#e74c3c' if avg_rate >= 0 else '#3498db'}; font-weight: bold;'>{avg_rate:+.2f}%</td></tr>"
                html_body_content += "</table>"
            else:
                html_body_content += "<p>해당 기간 내 매도 기록이 없습니다.</p>"
            
            # --- 월간 성장률 그래프 추가 ---
            graph_buffer = _generate_monthly_profit_graph(market, first_day_prev_month, last_day_prev_month)
            if graph_buffer:
                image_cid = f'monthly_profit_graph_{market}'
                img = MIMEImage(graph_buffer.getvalue(), 'png')
                img.add_header('Content-ID', f'<{image_cid}>') # Content-ID must be enclosed in angle brackets
                msg_root.attach(img)
                html_body_content += f'<br><h3>📈 {market} 시장 월간 누적 실현 손익 그래프</h3>'
                html_body_content += f'<img src="cid:{image_cid}" alt="{market} 월간 누적 실현 손익 그래프" style="max-width:100%; height:auto;"><br>'

        except Exception as e:
            logger.error(f"[Email] {market} 월간 리포트 생성 실패: {e}")
            html_body_content += f"<p>{market} 시장 정보를 가져오는데 실패했습니다.</p>"
    
    html_body_content += """
        <hr>
        <p style="font-size: 12px; color: #888;">본 메일은 CAVR 시스템에서 자동 발송되었습니다.</p>
    </body>
    </html>
    """
    # Attach the HTML content as the main part of the multipart/related message
    html_part = MIMEText(html_body_content, 'html', 'utf-8')
    msg_root.attach(html_part)
    return msg_root # Return the MIMEMultipart object directly

def job_send_monthly_report():
    """스케줄러에서 매월 1일 호출"""
    now = datetime.now()
    first_day_this_month = now.replace(day=1)
    last_day_prev_month = first_day_this_month - timedelta(days=1)
    month_str = last_day_prev_month.strftime("%Y년 %m월")
    
    subject = f"📊 [CAVR] {month_str} 월간 투자 결산 리포트" # Subject should be a string
    msg_root_object = generate_monthly_report_html() # Get the MIMEMultipart object
    send_email(subject, msg_root_object) # Pass the MIMEMultipart object

def format_currency_simple(val, market):
    """HTML 보고서용 간단 포맷팅"""
    if market == "KR": return f"{int(val):,}"
    return f"{val:,.2f}"