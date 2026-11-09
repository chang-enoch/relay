// The three-bar Relay mark, used in the header and as Relay's avatar.
export function RelayMark({ width, height, className }: { width: number; height: number; className?: string }) {
  return (
    <svg width={width} height={height} viewBox="0 0 20 26" aria-hidden className={className}>
      <rect x="0" y="6" width="5" height="14" rx="2.5" fill="var(--moss)" />
      <rect x="7.5" y="0" width="5" height="26" rx="2.5" fill="var(--moss)" />
      <rect x="15" y="5" width="5" height="16" rx="2.5" fill="var(--moss)" />
    </svg>
  );
}

export function Brand({ right }: { right?: React.ReactNode }) {
  return (
    <header className="flex items-center justify-between border-b border-line bg-card px-5 py-3 sm:px-8">
      <div className="flex items-baseline gap-3">
        <span className="flex items-center gap-2 text-3xl font-bold tracking-tight text-ink">
          <RelayMark width={20} height={26} className="translate-y-0.5" />
          relay
        </span>
        <span className="hidden text-muted sm:inline">/ team voice</span>
      </div>
      {right}
    </header>
  );
}
