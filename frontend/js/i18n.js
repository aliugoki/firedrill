/**
 * English and Arabic. Every string lives in both, and a missing one is a bug
 * rather than a silent fallback to English.
 *
 * `t()` returns the key itself when a string is missing, which is ugly on
 * purpose: an untranslated label should be obvious to whoever is testing the
 * Arabic build, not quietly readable because English leaked through.
 */

export const STRINGS = {
  en: {
    'app.title': 'EVAC-120',
    'app.safety_notice':
      'Supplements, never replaces, certified fire and life-safety systems. ' +
      'The warden’s physical count is the final authority.',

    'state.ACCOUNTED': 'Accounted for',
    'state.EVACUATING': 'Evacuating',
    'state.NOT_EVACUATED': 'Not evacuated',
    'state.UNCERTAIN': 'Uncertain',
    'state.UNACCOUNTED': 'Unaccounted for',
    'state.MANUAL_VERIFICATION_REQUIRED': 'Needs a person to check',

    'board.expected': 'Expected',
    'board.accounted': 'Accounted for',
    'board.uncertain': 'Uncertain',
    'board.unobserved': 'Currently unobserved',
    'board.unaccounted': 'Unaccounted for',
    'board.unknown': 'Unknown people',
    'board.needs_human': 'No camera can settle this — find them',
    'board.no_data_yet': 'no data has been received yet',
    // Why a person is in the state they are in. The server sends a code and
    // the values; the wording is here, because this line is read in Arabic on
    // a tablet at an assembly point and it is what tells a warden what to do.
    'reason.IDENTITY_DISPUTED': 'The system cannot tell who this is',
    'reason.WARDEN_REJECTED_IDENTITY': 'A warden said this is the wrong person',
    'reason.WARDEN_CONFIRMED_AT_ASSEMBLY': 'Confirmed in person by',
    'reason.ASSEMBLY_WITH_IDENTITY': 'Seen at the assembly point and identified',
    'reason.ASSEMBLY_WITH_IDENTITY_STALE': 'Identified earlier, face not visible now',
    'reason.ASSEMBLY_WITHOUT_IDENTITY': 'Somebody is at the assembly point, but not confirmed as this person',
    'reason.COVERAGE_DEGRADED': 'Cannot decide while the system cannot see',
    'reason.NOT_ON_ROSTER': 'Not on the roster — tag as a visitor or contractor',
    'reason.WARDEN_MARKED_ABSENT': 'Reported as not on site today by',
    'reason.TRACK_LOST': 'The system lost track of them',
    'reason.BRIEFLY_UNOBSERVED': 'Briefly out of sight',
    'reason.MOVING_THROUGH_EXIT': 'Moving through an exit',
    'reason.INSIDE_OVERDUE': 'Still inside',
    'reason.INSIDE': 'Inside the building',
    'reason.NEVER_OBSERVED_OVERDUE': 'Never seen by any camera',
    'reason.NEVER_OBSERVED': 'On the roster, not yet seen',
    'reason.last_seen': 'last seen in',
    'reason.on_camera': 'on',
    'reason.seconds_in': 's into the drill',
    // Why the board is refusing an all-clear. The commander reads this list
    // before deciding whether to keep two hundred people standing outside.
    'blocker.DRILL_NOT_STARTED': 'The drill has not been started',
    'blocker.NO_EXPECTED_PEOPLE': 'Nobody is expected on the roster',
    'blocker.PEOPLE_UNACCOUNTED': 'not accounted for',
    'blocker.OPEN_OUTAGES': 'outage(s) still open',
    'blocker.ROSTER_UNVERIFIED': 'The roster could not be verified against its source',
    'blocker.NO_ZONES': 'No zone has anybody expected at it',
    'blocker.UNASSIGNED_PEOPLE': 'have no assembly zone, so nobody can sweep for them',
    'blocker.NO_SWEEP_STARTED': 'no warden has started a sweep',
    'blocker.SWEEP_NOT_STARTED': 'the sweep has not been started',
    'blocker.SWEEP_IN_PROGRESS': 'the sweep is still in progress',
    'blocker.SWEEP_ESCALATED': 'escalated',
    'blocker.ZONE_UNCONFIRMED': 'not confirmed or reported',
    'blocker.NO_HEADCOUNT': 'no physical headcount yet',
    'blocker.HEADCOUNT_MISMATCH': 'the count disagrees with the system',
    'blocker.of': 'of',
    'blocker.people': 'people',


    'board.blinded_by': 'The system could not see here:',
    'board.evacuating': 'Still evacuating',
    'board.all_clear': 'ALL CLEAR',
    'board.not_all_clear': 'NOT ALL CLEAR',
    'board.blocking': 'Why not',
    'board.elapsed': 'Elapsed',
    'board.priority': 'Needs attention',
    'board.zones': 'Assembly zones',
    'board.health': 'System health',
    'board.timing': 'Evacuation times',
    'board.last_seen': 'Last seen',
    'board.no_priority': 'Nobody is waiting on a check.',

    'health.ok': 'All systems reporting',
    'health.degraded': 'Degraded',
    'health.blind': 'Cannot see',
    'health.outages': 'outages',

    'timing.p50': 'Median',
    'timing.p95': '95th percentile',
    'timing.target': 'Target',
    'timing.unreliable': 'Too few measurements to be a distribution',
    'timing.coverage_low': 'of people produced no timing at all',
    'timing.none': 'No measurements yet',
    'timing.completion': 'Accountability settled after',

    'bottleneck.title': 'Exits',
    'bottleneck.limiting': 'Slowest exit',
    'bottleneck.none_limiting': 'No exit is holding anyone up',
    'bottleneck.none': 'No exit has been measured yet',
    'bottleneck.through': 'through',
    'bottleneck.queue': 'in the zone',
    'bottleneck.dwell': 'median time to clear',
    'bottleneck.not_measured': 'not measured',

    'drill.list': 'Drills',
    'drill.create': 'New drill',
    'drill.start': 'Start drill',
    'drill.complete': 'End drill',
    'drill.name': 'Name',
    'drill.status.DRAFT': 'Not started',
    'drill.status.RUNNING': 'Running',
    'drill.status.COMPLETE': 'Finished',
    'drill.none': 'No drill',
    'drill.complete_confirm': 'End it — press again',
    'drill.still_outstanding': 'Still outstanding',

    'warden.my_zone': 'My zone',
    'warden.roster': 'People',
    'warden.unknown': 'Unknown people',
    'explain.claimed_as': 'The system cannot tell whether this is',
    'warden.cannot_save': 'THIS DEVICE CANNOT SAVE YOUR WORK — report by radio',
    'warden.no_answer': 'action(s) got no answer and are still queued',
    'warden.never_connected': 'No warden device has connected',
    'warden.silent_for': 'Warden device silent for',
    'warden.sweep': 'Finish sweep',
    'warden.search': 'Search',
    'warden.headcount': 'Physical headcount',
    'warden.headcount_prompt': 'How many people can you count?',
    'warden.headcount_submit': 'Submit count',
    'warden.confirm': 'Confirm present',
    'warden.not_here': 'Not here',
    'warden.wrong_person': 'Wrong person',
    'warden.mark_absent': 'Not on site today',
    'warden.tag_visitor': 'Visitor',
    'warden.tag_contractor': 'Contractor',
    'warden.escalate': 'Escalate',
    'warden.note': 'Add a note',
    'warden.words_needed': 'Say what is wrong. Nobody can act on a blank one.',
    'warden.send': 'Send',
    'warden.cancel': 'Cancel',
    'warden.offline': 'Offline — your work is saved on this device',
    'warden.online': 'Online',
    'warden.pending': 'waiting to sync',
    'warden.remembered': 'Remembered roster — not live',
    'warden.synced': 'All work synced',
    'warden.outstanding': 'Still to check',
    'warden.confirmed': 'Confirmed',
    'warden.match': 'Your count matches the system',
    'warden.mismatch_over': 'The system counted more than you did',
    'warden.mismatch_under': 'You counted more than the system knows about',
    'warden.sweep_done': 'Sweep finished',
    'warden.rely_on_count':
      'The system cannot see properly. Rely on your own count.',
  },
  ar: {
    'app.title': 'إيفاك-١٢٠',
    'app.safety_notice':
      'يكمّل أنظمة الإطفاء والسلامة المعتمدة ولا يحل محلها. العدّ اليدوي ' +
      'الذي يجريه المسؤول هو المرجع النهائي.',

    'state.ACCOUNTED': 'تم التأكد من سلامته',
    'state.EVACUATING': 'قيد الإخلاء',
    'state.NOT_EVACUATED': 'لم يُخلَ بعد',
    'state.UNCERTAIN': 'غير مؤكد',
    'state.UNACCOUNTED': 'غير محسوب',
    'state.MANUAL_VERIFICATION_REQUIRED': 'يحتاج تحققاً بشرياً',

    'board.expected': 'المتوقع',
    'board.accounted': 'تم التأكد',
    'board.uncertain': 'غير مؤكد',
    'board.unobserved': 'غير مرئي حالياً',
    'board.unaccounted': 'غير محسوب',
    'board.unknown': 'أشخاص غير معروفين',
    'board.needs_human': 'لا يمكن لأي كاميرا تأكيد هذا الشخص — ابحث عنه',
    'board.no_data_yet': 'لم تصل أي بيانات بعد',
    'reason.IDENTITY_DISPUTED': 'لا يستطيع النظام تحديد هوية هذا الشخص',
    'reason.WARDEN_REJECTED_IDENTITY': 'قال المراقب إن هذا شخص آخر',
    'reason.WARDEN_CONFIRMED_AT_ASSEMBLY': 'تأكيد شخصي من',
    'reason.ASSEMBLY_WITH_IDENTITY': 'شوهد في نقطة التجمع وتم التعرف عليه',
    'reason.ASSEMBLY_WITH_IDENTITY_STALE': 'تم التعرف عليه سابقاً، والوجه غير ظاهر الآن',
    'reason.ASSEMBLY_WITHOUT_IDENTITY': 'يوجد شخص في نقطة التجمع، لكن لم يتأكد أنه هذا الشخص',
    'reason.COVERAGE_DEGRADED': 'لا يمكن الحكم والنظام لا يرى',
    'reason.NOT_ON_ROSTER': 'غير مدرج في القائمة — صنّفه زائراً أو متعاقداً',
    'reason.WARDEN_MARKED_ABSENT': 'أُبلغ أنه ليس في الموقع اليوم من',
    'reason.TRACK_LOST': 'فقد النظام أثره',
    'reason.BRIEFLY_UNOBSERVED': 'خارج المجال لفترة قصيرة',
    'reason.MOVING_THROUGH_EXIT': 'يتحرك عبر مخرج',
    'reason.INSIDE_OVERDUE': 'ما زال في الداخل',
    'reason.INSIDE': 'داخل المبنى',
    'reason.NEVER_OBSERVED_OVERDUE': 'لم تره أي كاميرا',
    'reason.NEVER_OBSERVED': 'مدرج في القائمة ولم يُشاهد بعد',
    'reason.last_seen': 'آخر ظهور في',
    'reason.on_camera': 'على',
    'reason.seconds_in': 'ثانية من بدء التمرين',
    'blocker.DRILL_NOT_STARTED': 'لم يبدأ التمرين بعد',
    'blocker.NO_EXPECTED_PEOPLE': 'لا أحد متوقع في القائمة',
    'blocker.PEOPLE_UNACCOUNTED': 'غير محسوبين',
    'blocker.OPEN_OUTAGES': 'عطل ما زال قائماً',
    'blocker.ROSTER_UNVERIFIED': 'تعذّر التحقق من القائمة مع مصدرها',
    'blocker.NO_ZONES': 'لا توجد نقطة تجمع متوقع فيها أحد',
    'blocker.UNASSIGNED_PEOPLE': 'بلا نقطة تجمع، فلا يستطيع أحد تفقدهم',
    'blocker.NO_SWEEP_STARTED': 'لم يبدأ أي مراقب التفقد',
    'blocker.SWEEP_NOT_STARTED': 'لم يبدأ التفقد',
    'blocker.SWEEP_IN_PROGRESS': 'التفقد ما زال جارياً',
    'blocker.SWEEP_ESCALATED': 'تم التصعيد',
    'blocker.ZONE_UNCONFIRMED': 'لم يتم تأكيدهم أو الإبلاغ عنهم',
    'blocker.NO_HEADCOUNT': 'لا يوجد عدّ يدوي بعد',
    'blocker.HEADCOUNT_MISMATCH': 'العدّ يخالف النظام',
    'blocker.of': 'من',
    'blocker.people': 'شخصاً',


    'board.blinded_by': 'لم يتمكن النظام من الرؤية هنا:',
    'board.evacuating': 'ما زال يُخلي',
    'board.all_clear': 'الوضع آمن',
    'board.not_all_clear': 'الوضع غير آمن',
    'board.blocking': 'الأسباب',
    'board.elapsed': 'الوقت المنقضي',
    'board.priority': 'يحتاج انتباهاً',
    'board.zones': 'نقاط التجمع',
    'board.health': 'حالة النظام',
    'board.timing': 'أوقات الإخلاء',
    'board.last_seen': 'آخر ظهور',
    'board.no_priority': 'لا أحد ينتظر التحقق.',

    'health.ok': 'جميع الأنظمة تعمل',
    'health.degraded': 'أداء منخفض',
    'health.blind': 'لا يمكن الرؤية',
    'health.outages': 'انقطاعات',

    'timing.p50': 'الوسيط',
    'timing.p95': 'المئين ٩٥',
    'timing.target': 'الهدف',
    'timing.unreliable': 'القياسات قليلة جداً لتكوّن توزيعاً',
    'timing.coverage_low': 'من الأشخاص لم يُنتجوا أي قياس',
    'timing.none': 'لا قياسات بعد',
    'timing.completion': 'اكتمل الحصر بعد',

    'bottleneck.title': 'المخارج',
    'bottleneck.limiting': 'أبطأ مخرج',
    'bottleneck.none_limiting': 'لا مخرج يعيق أحداً',
    'bottleneck.none': 'لم يُقَس أي مخرج بعد',
    'bottleneck.through': 'عبروا',
    'bottleneck.queue': 'في المنطقة',
    'bottleneck.dwell': 'الزمن الوسيط للعبور',
    'bottleneck.not_measured': 'غير مقاس',

    'drill.list': 'التمارين',
    'drill.create': 'تمرين جديد',
    'drill.start': 'ابدأ التمرين',
    'drill.complete': 'أنهِ التمرين',
    'drill.name': 'الاسم',
    'drill.status.DRAFT': 'لم يبدأ',
    'drill.status.RUNNING': 'جارٍ',
    'drill.status.COMPLETE': 'انتهى',
    'drill.none': 'لا تمرين',
    'drill.complete_confirm': 'أنهِ — اضغط مرة أخرى',
    'drill.still_outstanding': 'ما زال معلقاً',

    'warden.my_zone': 'منطقتي',
    'warden.roster': 'الأشخاص',
    'warden.unknown': 'أشخاص غير معروفين',
    'explain.claimed_as': 'لا يستطيع النظام التمييز بين',
    'warden.cannot_save': 'هذا الجهاز لا يستطيع حفظ عملك — أبلغ عبر اللاسلكي',
    'warden.no_answer': 'إجراء بلا رد ولا يزال في قائمة الانتظار',
    'warden.never_connected': 'لم يتصل أي جهاز مراقب',
    'warden.silent_for': 'جهاز المراقب صامت منذ',
    'warden.sweep': 'إنهاء التفقد',
    'warden.search': 'بحث',
    'warden.headcount': 'العدّ اليدوي',
    'warden.headcount_prompt': 'كم شخصاً تعدّ أمامك؟',
    'warden.headcount_submit': 'أرسل العدد',
    'warden.confirm': 'تأكيد الحضور',
    'warden.not_here': 'غير موجود',
    'warden.wrong_person': 'شخص خاطئ',
    'warden.mark_absent': 'ليس في الموقع اليوم',
    'warden.tag_visitor': 'زائر',
    'warden.tag_contractor': 'مقاول',
    'warden.escalate': 'تصعيد',
    'warden.note': 'إضافة ملاحظة',
    'warden.words_needed': 'اكتب ما الخطب. لا أحد يستطيع التصرف بناءً على فراغ.',
    'warden.send': 'إرسال',
    'warden.cancel': 'إلغاء',
    'warden.offline': 'غير متصل — عملك محفوظ على هذا الجهاز',
    'warden.online': 'متصل',
    'warden.pending': 'بانتظار المزامنة',
    'warden.remembered': 'قائمة محفوظة — غير محدَّثة',
    'warden.synced': 'تمت مزامنة كل العمل',
    'warden.outstanding': 'ما زال للتفقد',
    'warden.confirmed': 'مؤكد',
    'warden.match': 'عددك يطابق النظام',
    'warden.mismatch_over': 'النظام عدّ أكثر منك',
    'warden.mismatch_under': 'عددت أكثر مما يعرفه النظام',
    'warden.sweep_done': 'انتهى التفقد',
    'warden.rely_on_count': 'النظام لا يرى بوضوح. اعتمد على عدّك أنت.',
  },
};

export const RTL_LANGUAGES = new Set(['ar']);

export function createTranslator(lang) {
  const table = STRINGS[lang] || STRINGS.en;
  return function t(key, fallback) {
    if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
    // Deliberately not falling back to English: an untranslated label must be
    // obvious to whoever is testing the Arabic build.
    return fallback ?? key;
  };
}

export function isRtl(lang) {
  return RTL_LANGUAGES.has(lang);
}

/** Every key present in every language. A missing string is a bug. */
export function missingKeys() {
  const all = new Set();
  for (const table of Object.values(STRINGS)) {
    for (const key of Object.keys(table)) all.add(key);
  }
  const missing = {};
  for (const [lang, table] of Object.entries(STRINGS)) {
    const gaps = [...all].filter((key) => !(key in table));
    if (gaps.length) missing[lang] = gaps;
  }
  return missing;
}

export function formatTime(ms, lang) {
  if (ms === null || ms === undefined) return '—';
  return new Intl.DateTimeFormat(lang === 'ar' ? 'ar-EG' : 'en-GB', {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(ms));
}

export function formatDuration(ms, lang) {
  if (ms === null || ms === undefined) return '—';
  const total = Math.max(0, Math.round(ms / 1000));
  const minutes = String(Math.floor(total / 60)).padStart(2, '0');
  const seconds = String(total % 60).padStart(2, '0');
  return `${minutes}:${seconds}`;
}
