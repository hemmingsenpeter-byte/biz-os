"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError, errorMessage } from "./api";

export function useData<T>(path: string) {
  const router = useRouter();
  const [result, setResult] = useState<{ path: string; value: T } | null>(null);
  const current = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const reload = useCallback(async () => {
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    setLoading(true);
    setError("");
    try {
      const value = await api<T>(path, { signal: controller.signal });
      if (!controller.signal.aborted) setResult({ path, value });
    } catch (err) {
      if (controller.signal.aborted) return;
      if (err instanceof ApiError && err.status === 401)
        router.replace("/auth/login");
      else setError(errorMessage(err));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [path, router]);
  useEffect(() => {
    // Fetching is an external synchronization; reload also serves explicit user refreshes.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void reload();
    return () => current.current?.abort();
  }, [reload]);
  return {
    data: result?.path === path ? result.value : null,
    loading,
    error,
    reload,
  };
}
