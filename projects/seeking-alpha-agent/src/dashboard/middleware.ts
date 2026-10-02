import { NextResponse, type NextRequest } from "next/server";

/** Access gate: when DASHBOARD_ACCESS_KEY is set, the first visit needs `?key=<it>`; a cookie keeps you in for 30 days.
 *  Not an identity system — one shared key for one person's read-only page. The health route is open (no data). */
const COOKIE = "saa_dash";

export function middleware(req: NextRequest) {
  const expected = process.env.DASHBOARD_ACCESS_KEY || "";
  if (!expected) return NextResponse.next();
  const url = req.nextUrl;
  if (url.pathname.startsWith("/api/health")) return NextResponse.next();
  const given = url.searchParams.get("key");
  if (given !== null) {
    if (timingSafeEqual(given, expected)) {
      url.searchParams.delete("key");
      const res = NextResponse.redirect(url);
      res.cookies.set(COOKIE, expected, { httpOnly: true, sameSite: "lax", secure: url.protocol === "https:", maxAge: 60 * 60 * 24 * 30, path: "/" });
      return res;
    }
    return new NextResponse("forbidden", { status: 403 });
  }
  const cookie = req.cookies.get(COOKIE)?.value || "";
  if (timingSafeEqual(cookie, expected)) return NextResponse.next();
  return new NextResponse("This dashboard needs an access key: open it as /?key=<DASHBOARD_ACCESS_KEY> once.", { status: 401, headers: { "Content-Type": "text/plain" } });
}

function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
