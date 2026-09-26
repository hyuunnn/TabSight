import fs from 'node:fs';
const src='node_modules/@coderline/alphatab/dist';const dest='public/alphatab';
fs.mkdirSync(dest,{recursive:true});
for(const name of fs.readdirSync(src)) if(name.endsWith('.mjs')||['font','soundfont'].includes(name)) fs.cpSync(`${src}/${name}`,`${dest}/${name}`,{recursive:true});
