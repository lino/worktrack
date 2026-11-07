/**
 * Duration balancing, `Intl.DurationFormat` human formatting, and ISO 8601 `<time datetime>`
 * serialization helpers (`calculate-event-differentials`).
 */
import { DURATION_CONSTANTS } from '../config/constants';

/** Balanced hours, minutes, and seconds breakdown. */
export interface BalancedDurationParts {
  hours: number;
  minutes: number;
  seconds: number;
}

/**
 * Balance a raw duration in seconds into non-negative integer hours, minutes, and seconds.
 * @param totalSeconds Raw duration in seconds.
 * @returns Balanced `{ hours, minutes, seconds }` structure.
 */
export function balanceDurationSeconds(totalSeconds: number | null | undefined): BalancedDurationParts {
  const clamped = Math.max(0, Math.round(totalSeconds ?? 0));
  const hours = Math.floor(clamped / DURATION_CONSTANTS.secondsPerHour);
  const remainderAfterHours = clamped % DURATION_CONSTANTS.secondsPerHour;
  const minutes = Math.floor(remainderAfterHours / DURATION_CONSTANTS.secondsPerMinute);
  const seconds = remainderAfterHours % DURATION_CONSTANTS.secondsPerMinute;
  return { hours, minutes, seconds };
}

/**
 * Serialize a duration in seconds as a machine-readable ISO 8601 duration string (`PT1H30M`).
 * @param totalSeconds Duration in seconds.
 * @returns ISO 8601 duration string suitable for `<time :datetime="...">`.
 */
export function formatIsoDuration(totalSeconds: number | null | undefined): string {
  const { hours, minutes, seconds } = balanceDurationSeconds(totalSeconds);
  if (hours === 0 && minutes === 0 && seconds === 0) {
    return 'PT0S';
  }
  const chunks: string[] = ['PT'];
  if (hours > 0) {
    chunks.push(`${hours}H`);
  }
  if (minutes > 0) {
    chunks.push(`${minutes}M`);
  }
  if (seconds > 0) {
    chunks.push(`${seconds}S`);
  }
  return chunks.join('');
}

/**
 * Format a duration in seconds for human display using `Intl.DurationFormat` with balanced units.
 * @param totalSeconds Duration in seconds.
 * @param locale Optional BCP 47 locale tag (defaults to `en-US`).
 * @returns Localized human-readable duration string.
 */
export function formatHumanDuration(
  totalSeconds: number | null | undefined,
  locale: string = DURATION_CONSTANTS.defaultLocale,
): string {
  const parts = balanceDurationSeconds(totalSeconds);
  const intlWithDuration = Intl as unknown as {
    DurationFormat?: new (
      loc: string,
      opts: { style: 'long' | 'short' | 'narrow' | 'digital' },
    ) => { format: (d: BalancedDurationParts) => string };
  };

  if (typeof intlWithDuration.DurationFormat === 'function') {
    const formatter = new intlWithDuration.DurationFormat(locale, { style: 'short' });
    return formatter.format(parts);
  }
  return `${parts.hours} hr, ${parts.minutes} min`;
}
