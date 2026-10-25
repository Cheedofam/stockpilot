import {
  BarChart3, BookOpen, Building2, ExternalLink, Landmark, LineChart,
  PieChart, Scale, Shield, ShieldCheck, Wallet,
} from "lucide-react";
import { Disclaimer } from "../components/ui.jsx";

const TOPICS = [
  {
    icon: <Landmark size={17} />,
    title: "What is insider trading disclosure?",
    body: `Corporate insiders, members of Congress, and large fund managers
must publicly report their stock trades. Congress members file Periodic
Transaction Reports within 45 days under the STOCK Act; corporate insiders
file SEC Form 4 within 2 business days. StockPilot reads these public
filings — by the time you see a trade, the trader made it days to weeks
ago. That lag is the most important caveat in this entire space.`,
  },
  {
    icon: <BookOpen size={17} />,
    title: "What is a 13F filing?",
    body: `Funds managing over $100M must file a quarterly form 13F with the
SEC listing their US stock holdings, up to 45 days after quarter end.
Comparing two consecutive 13Fs shows what billionaires like Warren Buffett
or David Tepper bought and sold. It's a snapshot, not a live feed — a fund
may have already changed its position by the time the filing is public.`,
  },
  {
    icon: <BarChart3 size={17} />,
    title: "How to read a convergence signal",
    body: `StockPilot's score rises when multiple unrelated smart-money
buyers converge on the same stock in a short window. Several Congress
members AND several billionaire funds buying the same ticker is rarer —
and historically more interesting — than any single trade. A score over
70 means strong convergence; 50–70 is moderate; below 30 is weak. The
score measures disclosed buying interest, not future returns.`,
  },
  {
    icon: <LineChart size={17} />,
    title: "What is RSI?",
    body: `The Relative Strength Index measures how fast a stock has been
rising or falling over the past 14 days, on a 0–100 scale. Above ~70 the
stock is considered "overbought" (the move may be stretched); below ~30
it's "oversold". RSI describes momentum, not value — overbought stocks can
keep rising for a long time.`,
  },
  {
    icon: <PieChart size={17} />,
    title: "What is MACD?",
    body: `Moving Average Convergence Divergence compares a fast (12-day)
and a slow (26-day) moving average of price. When the fast average crosses
above the slow one, momentum is turning up; crossing below means turning
down. Like all momentum indicators, it lags price and gives false signals
in choppy markets — traders use it for confirmation, not prediction.`,
  },
  {
    icon: <Wallet size={17} />,
    title: "How much should a student invest?",
    body: `Only money you won't need for at least 3–5 years. A common rule:
build an emergency fund first (3 months of expenses), pay off high-interest
debt, then invest small regular amounts — even $25/month builds the habit.
Never invest money you need for tuition or rent, and never borrow to invest.
Time in the market matters more than the amount you start with.`,
  },
  {
    icon: <Scale size={17} />,
    title: "What is diversification?",
    body: `Spreading money across many stocks, sectors, and asset types so
that no single failure can wipe you out. A low-cost index fund holding
hundreds of companies is the simplest form. Following insider signals into
3–4 concentrated positions is the opposite of diversification — if you act
on signals at all, keep them a small slice of a diversified portfolio.`,
  },
  {
    icon: <Shield size={17} />,
    title: "Risk vs reward basics",
    body: `Higher potential returns always come with higher potential
losses — there is no exception, and anyone promising one is selling
something. Volatile stocks (StockPilot labels them HIGH risk) can move
±20% in days. Before any trade ask: how much can I lose, how likely is
that, and can I afford it? Politicians and billionaires lose money on
trades too — their disclosures are research input, not a cheat code.`,
  },
];

// All of these are SEC-registered broker-dealers, FINRA members, and SIPC
// members — the three layers that make a US brokerage "safe": regulation,
// oversight, and insurance if the broker itself fails.
const BROKERS = [
  {
    name: "Fidelity",
    url: "https://www.fidelity.com",
    blurb: "Best all-rounder for beginners. $0 commissions, no account "
      + "minimums, fractional shares from $1, and the strongest free "
      + "research tools of any retail broker.",
    why: "SEC-registered, FINRA member, SIPC-insured (up to $500K per "
      + "account, $250K cash) plus additional private excess insurance. "
      + "One of the largest US brokers, founded 1946.",
  },
  {
    name: "Charles Schwab",
    url: "https://www.schwab.com",
    blurb: "Huge, established broker with excellent education content. "
      + "\"Stock Slices\" let you buy fractions of S&P 500 companies "
      + "for as little as $5.",
    why: "SEC-registered, FINRA member, SIPC-insured plus excess coverage. "
      + "Publicly traded bank holding company managing ~$10T in client "
      + "assets, founded 1971.",
  },
  {
    name: "Vanguard",
    url: "https://www.vanguard.com",
    blurb: "The home of low-cost index funds — ideal for the long-term, "
      + "boring-but-effective core of a portfolio rather than active "
      + "trading.",
    why: "SEC-registered, FINRA member, SIPC-insured. Uniquely owned by "
      + "its own funds' investors, which keeps fees among the lowest in "
      + "the industry. Founded 1975.",
  },
  {
    name: "Robinhood",
    url: "https://robinhood.com",
    blurb: "Simplest app experience and fractional shares — a common "
      + "first broker. Be aware the interface can make trading feel like "
      + "a game; trade less, not more.",
    why: "SEC-registered, FINRA member, SIPC-insured like the others — "
      + "your shares are protected if the broker fails (SIPC does NOT "
      + "protect against your investments losing value, at any broker).",
  },
  {
    name: "Interactive Brokers",
    url: "https://www.interactivebrokers.com",
    blurb: "Pro-grade platform with global market access and very low "
      + "costs. More complex than the others — better as a second broker "
      + "once you know what you're doing.",
    why: "SEC-registered, FINRA member, SIPC-insured plus excess "
      + "coverage. Publicly traded, founded 1978, serves clients in "
      + "200+ countries.",
  },
  {
    name: "Wealthsimple (Canada)",
    url: "https://www.wealthsimple.com",
    blurb: "If you're investing from Canada: commission-free Canadian and "
      + "US stocks, fractional shares, and TFSA/RRSP tax-sheltered "
      + "accounts.",
    why: "Regulated by CIRO (Canada's equivalent of FINRA) and CIPF-"
      + "insured up to $1M per account category if the broker fails.",
  },
];

const STEPS = [
  <>Pick a regulated broker from the list above. Verify any US broker
    yourself in 30 seconds with FINRA's free{" "}
    <a href="https://brokercheck.finra.org" target="_blank" rel="noopener noreferrer">
      BrokerCheck <ExternalLink size={11} /></a> — it shows registration
    status and disciplinary history. Confirm SIPC membership at{" "}
    <a href="https://www.sipc.org/list-of-members" target="_blank" rel="noopener noreferrer">
      sipc.org <ExternalLink size={11} /></a>.</>,
  <>Open the account on the broker's site or app (15–20 minutes). You'll
    need government ID, your SSN/SIN for tax reporting, and a bank account
    to link. Choose a standard individual brokerage account — skip margin
    and options approvals entirely as a beginner.</>,
  <>Deposit a small amount first — $50–100 — and wait for it to settle.
    Make sure two-factor authentication is turned on.</>,
  <>Make your first buy something diversified: a broad index ETF (e.g. one
    tracking the S&P 500) or a $5–10 fractional share of a company you
    researched. Use a <strong>limit order</strong> so you control the
    price you pay.</>,
  <>Only then experiment with individual picks. Use StockPilot's signals
    as a <em>starting point for research</em> — read the trades, the news,
    and the risk label — never as an instruction. Keep any single stock
    under ~5–10% of your portfolio.</>,
  <>Keep learning from neutral sources: the SEC's{" "}
    <a href="https://www.investor.gov" target="_blank" rel="noopener noreferrer">
      investor.gov <ExternalLink size={11} /></a> covers every basic
    honestly, with no product to sell you.</>,
];

export default function Learn() {
  return (
    <main className="page">
      <h1>Learn</h1>
      <p className="page-sub">
        The basics behind every number StockPilot shows, where to actually
        buy stocks safely, and how to make your first investment. None of
        this is investment advice — it's the vocabulary you need to evaluate
        the data yourself.
      </p>

      <h2><BookOpen size={16} /> Concepts</h2>
      <div className="card-grid">
        {TOPICS.map((t) => (
          <div className="card lift learn-card" key={t.title}>
            <h3>{t.icon} {t.title}</h3>
            <p className="small" style={{ opacity: 0.9, margin: 0 }}>{t.body}</p>
          </div>
        ))}
      </div>

      <h2><Building2 size={16} /> Where to buy stocks — and why these are safe</h2>
      <p className="muted small" style={{ marginTop: -4 }}>
        "Safe" here means three specific things: the broker is{" "}
        <strong>SEC/FINRA-regulated</strong> (or the Canadian equivalent), your
        assets are <strong>SIPC-insured</strong> if the broker itself fails
        (up to $500K, including $250K cash), and the firm is established with
        a long track record. No broker protects you from a stock going down —
        that risk is always yours.
      </p>
      <div className="card-grid">
        {BROKERS.map((b) => (
          <div className="card lift learn-card broker-card" key={b.name}>
            <h3>
              <Building2 size={16} /> {b.name}
              <a href={b.url} target="_blank" rel="noopener noreferrer"
                 style={{ marginLeft: "auto" }} title={`Open ${b.url}`}>
                <ExternalLink size={14} />
              </a>
            </h3>
            <p className="small" style={{ opacity: 0.9, margin: 0 }}>{b.blurb}</p>
            <div className="why">
              <ShieldCheck size={13} style={{ verticalAlign: "-2px" }} />{" "}
              <strong>Why it's safe:</strong> {b.why}
            </div>
          </div>
        ))}
      </div>

      <h2><Wallet size={16} /> How to make your first investment</h2>
      <div className="card">
        <ol className="step-list">
          {STEPS.map((s, i) => <li key={i}><div>{s}</div></li>)}
        </ol>
      </div>

      <h2><Shield size={16} /> Useful neutral resources</h2>
      <div className="card">
        <div className="feed-row">
          <a href="https://www.investor.gov" target="_blank" rel="noopener noreferrer">
            investor.gov</a>
          <span className="muted small">— the SEC's investor education site:
            how markets work, fraud red flags, compound-interest calculators.</span>
        </div>
        <div className="feed-row">
          <a href="https://brokercheck.finra.org" target="_blank" rel="noopener noreferrer">
            brokercheck.finra.org</a>
          <span className="muted small">— look up any US broker or financial
            adviser's license and disciplinary history before trusting them.</span>
        </div>
        <div className="feed-row">
          <a href="https://www.sipc.org" target="_blank" rel="noopener noreferrer">
            sipc.org</a>
          <span className="muted small">— what SIPC insurance does and
            doesn't cover, and the member list.</span>
        </div>
        <div className="feed-row">
          <a href="https://efts.sec.gov/LATEST/search-index?q=" target="_blank" rel="noopener noreferrer">
            SEC EDGAR full-text search</a>
          <span className="muted small">— read the raw 13F and Form 4 filings
            StockPilot's data comes from, straight from the source.</span>
        </div>
      </div>

      <Disclaimer />
    </main>
  );
}
