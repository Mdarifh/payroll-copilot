import { useCallback, useEffect, useState } from "react";

/** "#/employees" <-> "employees". Keeps the page in the URL so refresh / back work. */
export function useHashRoute<T extends string>(allowed: readonly T[], fallback: T) {
  const read = () => {
    const id = window.location.hash.replace(/^#\/?/, "") as T;
    return allowed.includes(id) ? id : fallback;
  };
  const [route, setRoute] = useState<T>(read);
  useEffect(() => {
    const onHash = () => setRoute(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  });
  // setting the same hash is a no-op, so this needs no dependency on the current route
  const go = useCallback((id: T) => { window.location.hash = `/${id}`; }, []);
  return [route, go] as const;
}
