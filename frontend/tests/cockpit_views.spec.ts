/**
 * Behavioural tests for the 4 Worktrack cockpit views, responsive `<table>` containers,
 * `Intl.DurationFormat` + ISO 8601 `<time datetime="...">` formatting, accessible forms,
 * and `deploy/alerts.yml` runbook anchor parity.
 */
import { mount } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import { createORM, useRepo } from 'pinia-orm';
import { beforeEach, describe, expect, it } from 'vitest';
import alertsYaml from '../../deploy/alerts.yml?raw';
import runbookMd from '../../docs/operations/runbook.md?raw';
import App from '../src/App.vue';
import { formatHumanDuration, formatIsoDuration } from '../src/composables/useDurationFormat';
import { IntervalModel, OutboxItemModel, seedCockpitDemoState } from '../src/models';

describe('Duration formatting and Cockpit Views', () => {
  beforeEach(() => {
    document.head.innerHTML = '<meta name="color-scheme" content="light dark" />';
    const pinia = createPinia().use(createORM());
    setActivePinia(pinia);
    seedCockpitDemoState();
  });

  it('formats durations with Intl.DurationFormat and machine-readable ISO 8601 strings', () => {
    expect(formatIsoDuration(5400)).toBe('PT1H30M');
    expect(formatIsoDuration(45)).toBe('PT45S');
    expect(formatIsoDuration(0)).toBe('PT0S');

    const human = formatHumanDuration(5400, 'en-US');
    expect(human).toContain('1');
    expect(human).toContain('30');
  });

  it('renders semantic landmarks, skip link, 4 cockpit views, responsive tables, form validation, and native dialogs', async () => {
    const wrapper = mount(App, {
      attachTo: document.body,
    });

    const skipLink = wrapper.find('a.skip-link.visually-hidden');
    expect(skipLink.exists()).toBe(true);
    expect(skipLink.attributes('href')).toBe('#cockpit-heading');

    const heading = wrapper.find('h1#cockpit-heading');
    expect(heading.exists()).toBe(true);
    expect(heading.attributes('tabindex')).toBe('-1');

    expect(wrapper.find('nav[aria-label="Primary"]').exists()).toBe(true);
    expect(wrapper.find('main#main-content').exists()).toBe(true);

    expect(wrapper.text()).toContain('Now & Insights');
    expect(wrapper.find('time[datetime^="PT"]').exists()).toBe(true);

    await wrapper.find('[data-testid="nav-agenda"]').trigger('click');
    expect(wrapper.text()).toContain('Today / Week Agenda & FocusBlocks');

    await wrapper.find('[data-testid="nav-capture"]').trigger('click');
    expect(wrapper.text()).toContain('Chaotic Interval & Attribution Editor');
    const activityInput = wrapper.find('input#activity-key-input');
    expect(activityInput.attributes('autocorrect')).toBe('off');
    expect(activityInput.attributes('autocapitalize')).toBe('off');
    expect(activityInput.attributes('spellcheck')).toBe('false');

    await activityInput.setValue('');
    await activityInput.trigger('blur');
    expect(wrapper.find('#activity-key-error').exists()).toBe(true);

    await activityInput.setValue('PROJ-142');
    await activityInput.trigger('input');
    expect(wrapper.find('#activity-key-error').exists()).toBe(false);

    const beforeCount = useRepo(IntervalModel).all().length;
    await wrapper.find('form.cockpit-form').trigger('submit');
    expect(useRepo(IntervalModel).all().length).toBe(beforeCount + 1);

    const tableRegion = wrapper.find('div.table-wrapper[role="region"][tabindex="0"]');
    expect(tableRegion.exists()).toBe(true);
    expect(tableRegion.find('table caption').exists()).toBe(true);
    expect(tableRegion.find('thead th[scope="col"]').exists()).toBe(true);
    expect(tableRegion.find('tbody th[scope="row"]').exists()).toBe(true);

    await wrapper.find('[data-testid="nav-outbox"]').trigger('click');
    expect(wrapper.text()).toContain('Outbox Diff Preview & EgressLedger');
    expect(wrapper.findAll('div.table-wrapper[role="region"]').length).toBeGreaterThanOrEqual(2);

    const approveBtn = wrapper.find('button[aria-label="Approve Outbox item PROJ-142"]');
    await approveBtn.trigger('click');
    expect(useRepo(OutboxItemModel).find('out-1')?.status).toBe('approved');

    const dialogs = wrapper.findAll('dialog[closedby="any"]');
    expect(dialogs.length).toBe(2);
    for (const dlg of dialogs) {
      expect(dlg.attributes('role')).toBeUndefined();
      expect(dlg.attributes('aria-modal')).toBeUndefined();
    }

    wrapper.unmount();
  });

  it('verifies deploy/alerts.yml runbook_url anchors resolve in docs/operations/runbook.md', () => {
    const normalizedRunbook = runbookMd.toLowerCase();
    const anchorMatches = [...alertsYaml.matchAll(/runbook_url:\s*"[^"#]+#([a-z0-9_-]+)"/g)];
    expect(anchorMatches.length).toBe(6);
    for (const match of anchorMatches) {
      const anchorSlug = match[1];
      expect(normalizedRunbook).toContain(`### ${anchorSlug}`);
    }
  });
});
