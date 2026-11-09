export function Brand({ right }: { right?: React.ReactNode }) {
  return (
    <header className="flex items-center justify-between border-b border-line bg-card px-5 py-3 sm:px-8">
      <div className="flex items-baseline gap-3">
        <span className="flex items-center gap-2 text-3xl font-bold tracking-tight text-ink">
          <svg width="20" height="26" viewBox="0 0 20 26" aria-hidden className="translate-y-0.5">
            <rect x="0" y="6" width="5" height="14" rx="2.5" fill="var(--moss)" />
            <rect x="7.5" y="0" width="5" height="26" rx="2.5" fill="var(--moss)" />
            <rect x="15" y="5" width="5" height="16" rx="2.5" fill="var(--moss)" />
          </svg>
          relay
        </span>
        <span className="hidden text-muted sm:inline">/ team voice</span>
      </div>
      {right}
    </header>
  );
}
