"use client";

import { useAuth } from "./AuthProvider";

export function UserMenu() {
  const { user, signOut } = useAuth();

  if (!user) return null;

  return (
    <div className="flex items-center gap-3 px-3 py-2 border-b border-zinc-800">
      {user.avatarUrl ? (
        <img
          src={user.avatarUrl}
          alt={user.displayName}
          className="w-8 h-8 rounded-full"
          referrerPolicy="no-referrer"
        />
      ) : (
        <div className="w-8 h-8 rounded-full bg-zinc-700 flex items-center justify-center text-sm text-white font-medium">
          {user.displayName[0]?.toUpperCase()}
        </div>
      )}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-white truncate">
            {user.displayName}
          </span>
          {user.role === "admin" && (
            <span className="text-[10px] font-semibold px-1.5 py-0.5 bg-amber-900/50 text-amber-300 border border-amber-800 rounded">
              ADMIN
            </span>
          )}
        </div>
        <span className="text-xs text-zinc-500 truncate block">
          {user.email}
        </span>
      </div>
      <button
        onClick={signOut}
        className="text-xs text-zinc-500 hover:text-white transition-colors"
        title="Sign out"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
          <polyline points="16 17 21 12 16 7" />
          <line x1="21" y1="12" x2="9" y2="12" />
        </svg>
      </button>
    </div>
  );
}
