#!/usr/bin/env node
// SPDX-License-Identifier: BUSL-1.1
// Copyright © 2024–2026 razzfazz.ai GmbH – Member of SEQIS Group.
//
// #2488: swap next/font/google (Geist, Geist Mono) for next/font/local in the
// upstream cognee-frontend layout, backed by the vendored woff2 files under
// frontend/fonts/ (../fonts/OFL.txt — SIL Open Font License 1.1). `next build`
// then makes NO network call to fonts.googleapis.com / fonts.gstatic.com — a
// single unreachable-DNS blip against that one host used to abort the whole
// `rzfz init` bake (measured on 0.91, 2026-09-25).
//
// Run against the git-cloned upstream source (modules/knowledge/cognee/
// frontend/Dockerfile, stage `source`), BEFORE `npm run build`, mirroring the
// sed FATAL-guard pattern already used elsewhere in that Dockerfile (#282,
// #2404): refuse to touch the file if the expected upstream text isn't there
// (upstream moved — re-audit), and refuse to finish if next/font/google
// survived the patch.
"use strict";
const fs = require("fs");

const FILE = "src/app/layout.tsx";

const IMPORT_OLD = 'import { Geist, Geist_Mono } from "next/font/google";';
const IMPORT_NEW = 'import localFont from "next/font/local";';

const FONTS_OLD = [
  "const geistSans = Geist({",
  '  variable: "--font-geist-sans",',
  '  subsets: ["latin"],',
  "});",
  "",
  "const geistMono = Geist_Mono({",
  '  variable: "--font-geist-mono",',
  '  subsets: ["latin"],',
  "});",
].join("\n");

// Paths are relative to THIS file (src/app/layout.tsx) per next/font/local's
// own resolution rule, landing on the fonts/ dir copied into the build
// context root (WORKDIR /app) by the Dockerfile: /app/src/app -> /app/fonts.
const FONTS_NEW = [
  "const geistSans = localFont({",
  '  src: "../../fonts/Geist-Variable.woff2",',
  '  variable: "--font-geist-sans",',
  '  weight: "100 900",',
  "});",
  "",
  "const geistMono = localFont({",
  '  src: "../../fonts/GeistMono-Variable.woff2",',
  '  variable: "--font-geist-mono",',
  '  weight: "100 900",',
  "});",
].join("\n");

const src = fs.readFileSync(FILE, "utf8");

if (!src.includes(IMPORT_OLD) || !src.includes(FONTS_OLD)) {
  console.error(
    "FATAL(#2488): expected next/font/google usage not found in " +
      FILE +
      " — cognee-frontend changed on this tag; re-audit the local-font patch."
  );
  process.exit(1);
}

const patched = src.replace(IMPORT_OLD, IMPORT_NEW).replace(FONTS_OLD, FONTS_NEW);

if (patched.includes("next/font/google") || patched.includes("Geist_Mono")) {
  console.error("FATAL(#2488): next/font/google survived the patch.");
  process.exit(1);
}

fs.writeFileSync(FILE, patched);
console.log(
  "OK(#2488): " + FILE + " now uses next/font/local (vendored Geist / Geist Mono, no network fetch at build)."
);
