import { useState } from "react";
import { downloadFile } from "../lib/api";
import { formatDays, formatINR } from "../lib/format";
import type { PayLine, Payslip } from "../lib/types";
import { DownloadIcon } from "./icons";

function Lines({ title, lines, totalLabel, total }: {
  title: string; lines: PayLine[]; totalLabel: string; total: string;
}) {
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h4>
      <dl className="space-y-1 text-sm">
        {lines.map((line) => (
          <div key={line.head_code} className="flex justify-between gap-4">
            <dt className="text-slate-600">{line.name}</dt>
            <dd className="tabular-nums text-slate-900">{formatINR(line.amount)}</dd>
          </div>
        ))}
        <div className="flex justify-between gap-4 border-t border-slate-200 pt-1 font-medium">
          <dt className="text-slate-700">{totalLabel}</dt>
          <dd className="tabular-nums text-slate-900">{formatINR(total)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function PayslipCard({ payslip, downloadUrl, token }: {
  payslip: Payslip; downloadUrl: string | null; token: string;
}) {
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lop = Number(payslip.lop_days) > 0;

  async function download() {
    if (!downloadUrl) return;
    setDownloading(true);
    setError(null);
    try {
      await downloadFile(token, downloadUrl,
        `payslip_${payslip.employee_code}_${payslip.period}.pdf`);
    } catch {
      setError("Could not download the PDF. Try again.");
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="w-full max-w-md overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-sm">
      <div className="flex items-start justify-between gap-3 border-b border-slate-100 px-4 py-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-emerald-700">Payslip</p>
          <h3 className="text-base font-semibold text-slate-900">{payslip.period_label}</h3>
          <p className="text-xs text-slate-500">
            {payslip.employee_name} · {payslip.employee_code}
          </p>
        </div>
        <div className="text-right text-xs text-slate-500">
          <p>Paid days <span className="font-medium text-slate-800">
            {formatDays(payslip.paid_days)}/{formatDays(payslip.basis_days)}</span></p>
          <p className={lop ? "font-medium text-amber-700" : ""}>LOP {formatDays(payslip.lop_days)}</p>
        </div>
      </div>

      <div className="grid gap-4 px-4 py-3 sm:grid-cols-2">
        <Lines title="Earnings" lines={payslip.earnings} totalLabel="Gross"
          total={payslip.gross_earnings} />
        <Lines title="Deductions" lines={payslip.deductions} totalLabel="Total"
          total={payslip.total_deductions} />
      </div>

      <div className="flex items-center justify-between gap-3 bg-emerald-50 px-4 py-3">
        <div>
          <p className="text-xs font-medium uppercase tracking-wide text-emerald-800">Net pay</p>
          <p className="text-xl font-semibold tabular-nums text-emerald-900">{formatINR(payslip.net_pay)}</p>
        </div>
        {downloadUrl && <button type="button" onClick={download} disabled={downloading}
          className="flex items-center gap-1.5 rounded-lg border border-emerald-600 bg-white px-3 py-1.5 text-sm font-medium text-emerald-700 hover:bg-emerald-600 hover:text-white disabled:opacity-60">
          <DownloadIcon />
          {downloading ? "Preparing…" : "Download PDF"}
        </button>}
      </div>
      {error && <p className="px-4 pb-3 text-sm text-red-600">{error}</p>}
    </div>
  );
}
