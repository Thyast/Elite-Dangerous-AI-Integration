const fs = require('fs');
const path = './src/app/services/i18n-translations.ts';
const src = fs.readFileSync(path, 'utf8');
const start = src.indexOf('{', src.indexOf('TRANSLATIONS'));
const end = src.lastIndexOf('}');
const tables = JSON.parse(src.slice(start, end + 1));

// English module names → src/assets/module_names.json (backend voice path)
const out = {
  schema_version: 1,
  source: 'i18n-translations.ts#module',
  generated_at: new Date().toISOString(),
  names: tables.en.module,
};

fs.writeFileSync('../src/assets/module_names.json', JSON.stringify(out, null, 2) + '\n');
console.log('module_names.json:', Object.keys(tables.en.module).length, 'entries');
