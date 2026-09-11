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

    'drill.list': 'Drills',
    'drill.create': 'New drill',
    'drill.start': 'Start drill',
    'drill.complete': 'End drill',
    'drill.name': 'Name',
    'drill.status.DRAFT': 'Not started',
    'drill.status.RUNNING': 'Running',
    'drill.status.COMPLETE': 'Finished',

    'warden.my_zone': 'My zone',
    'warden.roster': 'People',
    'warden.unknown': 'Unknown people',
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

    'drill.list': 'التمارين',
    'drill.create': 'تمرين جديد',
    'drill.start': 'ابدأ التمرين',
    'drill.complete': 'أنهِ التمرين',
    'drill.name': 'الاسم',
    'drill.status.DRAFT': 'لم يبدأ',
    'drill.status.RUNNING': 'جارٍ',
    'drill.status.COMPLETE': 'انتهى',

    'warden.my_zone': 'منطقتي',
    'warden.roster': 'الأشخاص',
    'warden.unknown': 'أشخاص غير معروفين',
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
