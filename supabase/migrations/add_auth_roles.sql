-- ============================================================================
-- Migration: Add Google OAuth support with admin/user roles
-- ============================================================================
-- Run this in Supabase SQL Editor BEFORE deploying the new code.
-- ============================================================================

-- 1. Add role and avatar columns to users table
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user' CHECK (role IN ('admin', 'user'));
ALTER TABLE public.users ADD COLUMN IF NOT EXISTS avatar_url TEXT;

-- 2. Create index on role for quick admin lookups
CREATE INDEX IF NOT EXISTS idx_users_role ON public.users(role);

-- 3. Update the users table to allow auth.users UUID as primary key
-- (Supabase Auth creates users in auth.users; we mirror them in public.users)
-- The existing UUID primary key already supports this — no schema change needed.

-- 4. Set known admins
UPDATE public.users SET role = 'admin' WHERE email IN (
    'jay98shinde@gmail.com',
    'kadirlofca@outlook.com'
);

-- 5. Create a function to auto-create public.users on signup
-- This trigger fires when a new user signs up via Supabase Auth (Google OAuth)
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS TRIGGER AS $$
DECLARE
    user_role TEXT;
BEGIN
    -- Check if the email is an admin
    IF NEW.email IN ('jay98shinde@gmail.com', 'kadirlofca@outlook.com') THEN
        user_role := 'admin';
    ELSE
        user_role := 'user';
    END IF;

    INSERT INTO public.users (id, email, display_name, avatar_url, role)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'full_name', NEW.raw_user_meta_data->>'name', split_part(NEW.email, '@', 1)),
        NEW.raw_user_meta_data->>'avatar_url',
        user_role
    )
    ON CONFLICT (id) DO UPDATE SET
        email = EXCLUDED.email,
        display_name = EXCLUDED.display_name,
        avatar_url = EXCLUDED.avatar_url,
        role = CASE
            WHEN EXCLUDED.email IN ('jay98shinde@gmail.com', 'kadirlofca@outlook.com') THEN 'admin'
            ELSE public.users.role  -- preserve existing role
        END,
        updated_at = NOW();

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- 6. Create trigger on auth.users
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT OR UPDATE ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
