"use strict";

const fs = require("node:fs");
const path = require("node:path");

const tag = process.env.GITHUB_REF_TYPE === "tag" ? process.env.GITHUB_REF_NAME : "";
if (!tag) process.exit(0);
if (!/^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(tag)) {
  throw new Error(`릴리즈 태그 형식이 올바르지 않습니다: ${tag}`);
}
const version = tag.slice(1);
for (const filename of ["package.json", "package-lock.json"]) {
  const location = path.resolve(filename);
  const document = JSON.parse(fs.readFileSync(location, "utf8"));
  document.version = version;
  if (document.packages && document.packages[""]) document.packages[""].version = version;
  fs.writeFileSync(location, `${JSON.stringify(document, null, 2)}\n`);
}
console.log(`desktop package version: ${version}`);
