/** The TaoScope mark, inline so it needs no request and scales crisply. Same
 *  geometry as public/logo.svg and app/icon.svg — change all three together. */
export function Logo({ size = 22, className }: { size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 512 512" width={size} height={size} className={className} aria-hidden>
      <rect width="512" height="512" rx="112" fill="#0f172a" />
      <g fill="none" stroke="#3b82f6" strokeWidth="26" strokeLinecap="round">
        <circle cx="256" cy="256" r="196" />
        <path d="M256 24V96M256 416v72M24 256h72M416 256h72" />
      </g>
      <g fill="none" stroke="#ffffff" strokeWidth="48" strokeLinecap="round" strokeLinejoin="round">
        <path d="M150 182h212" />
        <path d="M258 182v114c0 44 32 60 68 54" />
      </g>
    </svg>
  );
}
