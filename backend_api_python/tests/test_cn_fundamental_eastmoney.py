import pytest

from app.data_sources import cn_fundamental_history as history_module
from app.data_sources.cn_fundamental_history import fetch_eastmoney_annual_reports
from app.services import external_data_request_logs as request_log_module


@pytest.fixture(autouse=True)
def disable_eastmoney_wait(monkeypatch):
 class NoopLimiter:
  def wait(self): return 0.0

 monkeypatch.setattr(history_module, 'get_eastmoney_limiter', lambda: NoopLimiter())


class Response:
 def raise_for_status(self): pass
 def json(self): return {"result":{"data":[{"REPORT_DATE":"2025-12-31 00:00:00","NOTICE_DATE":"2026-04-17 00:00:00","UPDATE_DATE":"2026-04-17","SECUCODE":"600519.SH","TOTALOPERATEREVE":100,"PARENTNETPROFIT":20,"TOTAL_SHARE":10}]}}
class Session:
 def get(self,*args,**kwargs): return Response()
def test_maps_eastmoney_annual_row_with_notice_date():
 row=fetch_eastmoney_annual_reports('600519',Session())[0]
 assert row['period_end'].isoformat()=='2025-12-31'
 assert row['available_at'].isoformat()=='2026-04-17'
 assert row['fields']['revenue']==100.0
 assert row['fields']['parent_net_income']==20.0


class StatementSession:
 def get(self, url, params, **kwargs):
  class R(Response):
   def json(inner):
    if params.get('type'):
     return {'result': {'data': [{'REPORT_DATE':'2025-12-31','NOTICE_DATE':'2026-04-17','SECUCODE':'600519.SH','TOTAL_SHARE':10}]}}
    rows = {
     'RPT_DMSK_FN_INCOME': {'REPORT_DATE':'2025-12-31','OPERATE_INCOME':100,'OPERATE_COST':70,'PARENT_NETPROFIT':20},
     'RPT_DMSK_FN_CASHFLOW': {'REPORT_DATE':'2025-12-31','NETCASH_OPERATE':30,'CONSTRUCT_LONG_ASSET':8},
     'RPT_DMSK_FN_BALANCE': {'REPORT_DATE':'2025-12-31','TOTAL_EQUITY':200},
    }
    return {'result': {'data': [rows[params['reportName']]]}}
  return R()


def test_merges_statement_line_items_only_into_annual_main_periods():
 row = fetch_eastmoney_annual_reports('600519', StatementSession())[0]
 assert row['fields']['gross_profit'] == 30
 assert row['fields']['operating_cash_flow'] == 30
 assert row['fields']['capex_cash_paid'] == 8
 assert 'parent_equity_end' not in row['fields']


class RecordingService:
 def __init__(self): self.entries = []
 def record(self, entry): self.entries.append(entry); return True


def test_annual_backfill_records_each_eastmoney_provider_attempt(monkeypatch):
 service = RecordingService()
 monkeypatch.setattr(request_log_module, 'ExternalDataRequestLogService', lambda: service)
 fetch_eastmoney_annual_reports('600519', StatementSession())
 entries = [entry.normalized() for entry in service.entries]
 assert len(entries) == 4
 assert {entry['provider'] for entry in entries} == {'eastmoney'}
 assert {entry['data_domain'] for entry in entries} == {'fundamental_history'}
 assert {entry['call_source'] for entry in entries} == {'cn_fundamental_history'}
 assert {entry['operation'] for entry in entries} == {
  'annual_main_report', 'annual_income_statement',
  'annual_cashflow_statement', 'annual_balance_statement'
 }


def test_rate_limits_each_eastmoney_request(monkeypatch):
 class RecordingLimiter:
  def __init__(self): self.wait_count = 0
  def wait(self): self.wait_count += 1

 limiter = RecordingLimiter()
 monkeypatch.setattr(
  history_module, 'get_eastmoney_limiter', lambda: limiter, raising=False
 )

 fetch_eastmoney_annual_reports('600519', StatementSession())

 assert limiter.wait_count == 4
