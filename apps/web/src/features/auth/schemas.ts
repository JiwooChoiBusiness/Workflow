import * as z from "zod";
import { email, type T } from "@/lib/formSchemas";

// 서버 MAX_PASSWORD_LENGTH(app/security/passwords.py)
const PASSWORD_MAX = 256;

function newPassword(t: T, min: number) {
  return z
    .string()
    .min(min, { error: t("validation.minLength", { min }) })
    .max(PASSWORD_MAX, { error: t("validation.maxLength", { max: PASSWORD_MAX }) });
}

function optionalName(t: T) {
  return z
    .string()
    .trim()
    .max(100, { error: t("validation.maxLength", { max: 100 }) });
}

export function makeLoginSchema(t: T) {
  return z.object({
    email: email(t),
    password: z.string().min(1, { error: t("validation.required") }),
  });
}

export function makeSetupSchema(t: T, minLength: number) {
  return z
    .object({
      email: email(t),
      name: optionalName(t),
      workspace_name: optionalName(t),
      password: newPassword(t, minLength),
      password_confirm: z.string(),
    })
    .refine((value) => value.password === value.password_confirm, {
      path: ["password_confirm"],
      error: t("validation.passwordMismatch"),
    });
}

export function makePasswordChangeSchema(t: T, minLength: number) {
  return z
    .object({
      current_password: z.string().min(1, { error: t("validation.required") }),
      new_password: newPassword(t, minLength),
      new_password_confirm: z.string(),
    })
    .refine((value) => value.new_password === value.new_password_confirm, {
      path: ["new_password_confirm"],
      error: t("validation.passwordMismatch"),
    });
}

export type LoginForm = z.infer<ReturnType<typeof makeLoginSchema>>;
export type SetupForm = z.infer<ReturnType<typeof makeSetupSchema>>;
export type PasswordChangeForm = z.infer<ReturnType<typeof makePasswordChangeSchema>>;
